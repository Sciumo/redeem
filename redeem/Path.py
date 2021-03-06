""" 
Path.py - A single movement from one point to another 
All coordinates  in this file is in meters. 

Author: Elias Bakken
email: elias(dot)bakken(at)gmail(dot)com
Website: http://www.thing-printer.com
License: GNU GPL v3: http://www.gnu.org/copyleft/gpl.html

 Redeem is free software: you can redistribute it and/or modify
     it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
 
 Redeem is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.
 
 You should have received a copy of the GNU General Public License
 along with Redeem.  If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as np

from Delta import Delta
from BedCompensation import BedCompensation
import logging

class Path:
    AXES = "XYZEHABC"
    MAX_AXES = 8
    NUM_AXES = 5

    # Variables set from config.
    max_speeds             = [0]*MAX_AXES
    min_speeds             = [0]*MAX_AXES
    jerks                  = [0]*MAX_AXES
    acceleration           = [0]*MAX_AXES
    home_speed             = [0]*MAX_AXES
    home_backoff_speed     = [0]*MAX_AXES
    home_backoff_offset    = [0]*MAX_AXES
    steps_pr_meter         = [1]*MAX_AXES
    backlash_compensation  = [0]*MAX_AXES
    backlash_state         = [0]*MAX_AXES
    soft_min               = [0]*MAX_AXES
    soft_max               = [0]*MAX_AXES
    slaves                 = {key: "" for key in AXES}

    axes_zipped = ["X", "Y", "Z", "E", "H", "A", "B", "C"]

    AXIS_CONFIG_XY = 0
    AXIS_CONFIG_H_BELT = 1
    AXIS_CONFIG_CORE_XY = 2
    AXIS_CONFIG_DELTA = 3

    # Different types of paths
    ABSOLUTE = 0
    RELATIVE = 1
    G92 = 2
    G2 = 3
    G3 = 4

    # Numpy array type used throughout    
    DTYPE = np.float64

    # Precalculate the H-belt matrix
    matrix_H = np.matrix('-0.5 0.5; -0.5 -0.5')
    matrix_H_inv = np.linalg.inv(matrix_H)

    # Precalculate the CoreXY matrix
    # A - motor X (top right), B - motor Y (top left)
    # home located in bottom right corner    
    matrix_XY = np.matrix('1.0 1.0; 1.0 -1.0')
    matrix_XY_inv = np.linalg.inv(matrix_XY)

    # Unlevel bed compensation. 
    matrix_bed_comp     = np.identity(3)
    matrix_bed_comp_inv = np.linalg.inv(matrix_bed_comp)

    # Default config is normal cartesian XY
    axis_config = AXIS_CONFIG_XY 
    
    # By default, do not check for slaves
    has_slaves = False

    @staticmethod
    def add_slave(master, slave):
        ''' Make an axis copy the movement of another. 
        the slave will get the same position as the axis'''
        Path.slaves[master] = slave
        Path.has_slaves = True
    
    def __init__(self, axes, speed, accel, cancelable=False, use_bed_matrix=True, use_backlash_compensation=True, enable_soft_endstops=True):
        """ The axes of evil, the feed rate in m/s and ABS or REL """
        self.axes = axes
        self.speed = speed
        self.accel = accel
        self.cancelable = int(cancelable)
        self.use_bed_matrix = int(use_bed_matrix)
        self.use_backlash_compensation = int(use_backlash_compensation)
        self.enable_soft_endstops = enable_soft_endstops
        self.mag = None
        self.next = None
        self.prev = None
        self.speeds = None
        self.vec = None
        self.start_pos = None
        self.end_pos = None
        self.stepper_end_pos = None
        self.ideal_end_pos = None
        self.num_steps = None
        self.delta = None
        self.compensation = None
        self.split_size = 0.001       

    def is_G92(self):
        """ Special path, only set the global position on this """
        return self.movement == Path.G92

    def set_homing_feedrate(self):
        """ The feed rate is set to the lowest axis in the set """
        self.speeds = np.minimum(self.speeds,
                                 self.home_speed[np.argmax(self.vec)])
        self.speed = np.linalg.norm(self.speeds[:3])

    def unlink(self):
        """ unlink this from the chain. """
        self.next = None
        self.prev = None

    def transform_vector(self, vec, cur_pos):
        """ Transform vector to whatever coordinate system is used """
        ret_vec = np.copy(vec)
        if Path.axis_config == Path.AXIS_CONFIG_H_BELT:
            X = np.dot(Path.matrix_H_inv, vec[0:2])
            ret_vec[:2] = X[0]
        if Path.axis_config == Path.AXIS_CONFIG_CORE_XY:
            X = np.dot(Path.matrix_XY, vec[0:2])
            ret_vec[:2] = X[0]
        if Path.axis_config == Path.AXIS_CONFIG_DELTA:
            # Subtract the current column positions
            if hasattr(self.prev, "end_ABC"):
                self.start_ABC = self.prev.end_ABC
            else:
                self.start_ABC = Delta.inverse_kinematics2(cur_pos[0], cur_pos[1],
                                                 cur_pos[2])
            # Find the next column positions
            self.end_ABC = Delta.inverse_kinematics2(cur_pos[0] + vec[0],
                                               cur_pos[1] + vec[1],
                                               cur_pos[2] + vec[2])
            ret_vec[:3] = self.end_ABC - self.start_ABC
        return ret_vec

    def reverse_transform_vector(self, vec, cur_pos):
        """ Transform back from whatever """
        ret_vec = np.copy(vec)
        if Path.axis_config == Path.AXIS_CONFIG_H_BELT:
            X = np.dot(Path.matrix_H, vec[0:2])
            ret_vec[:2] = X[0]
        if Path.axis_config == Path.AXIS_CONFIG_CORE_XY:
            X = np.dot(Path.matrix_XY_inv, vec[0:2])
            ret_vec[:2] = X[0]
        if Path.axis_config == Path.AXIS_CONFIG_DELTA:
            # Find the next column positions
            self.end_ABC = self.start_ABC + vec[:3]

            # We have the column translations and need to find what that
            # represents in cartesian.
            start_xyz = Delta.forward_kinematics2(self.start_ABC[0], self.start_ABC[1],
                                                 self.start_ABC[2])
            end_xyz = Delta.forward_kinematics2(self.end_ABC[0], self.end_ABC[1],
                                               self.end_ABC[2])
            ret_vec[:3] = end_xyz - start_xyz
        return ret_vec

    @staticmethod
    def backlash_reset():
	    Path.backlash_state = np.zeros(Path.MAX_AXES)

    def backlash_compensate(self):
        """ Apply compensation to the distance taken if the direction of the axis has changed. """
        ret_vec = np.zeros(Path.MAX_AXES)
        if self.use_backlash_compensation:
            for index, d in enumerate(self.delta):
                dirstate = np.sign(d)
                #Compensate only if the direction has changed
                if (dirstate != 0) and (dirstate != Path.backlash_state[index]):
                    ret_vec[index] = dirstate * Path.backlash_compensation[index]
                    # Save new backlash state
                    Path.backlash_state[index] = dirstate

            if np.any(ret_vec):
                self.compensation = ret_vec

        return ret_vec

    def handle_tools(self):
        """ If tool is != E, move the vectors to the right position """
        if Path.printer.current_tool is not "E":
            tool = Path.printer.current_tool
            index = Path.axis_to_index(tool)
            self.start_pos[index] = self.start_pos[3]
            self.start_pos[3] = 0
            self.stepper_end_pos[index] = self.stepper_end_pos[3]
            self.stepper_end_pos[3] = 0

    def handle_slaves(self):
        # If slave mode is enabled, copy position now. 
        if Path.has_slaves:
            for slave in Path.slaves:
                master = Path.slaves[slave]
                if master:
                    s_i = Path.axis_to_index(slave)
                    m_i = Path.axis_to_index(master)
                    self.start_pos[s_i] = self.start_pos[m_i]
                    self.stepper_end_pos[s_i] = self.stepper_end_pos[m_i]

    def needs_splitting(self):
        #return False
        """ Return true if this is a delta segment and longer than 1 mm """
        # If there is no movement along the XY axis (Z+extruders) only, don't split.

        if self.movement == Path.G2 or self.movement == Path.G3:
            return True

        return (Path.axis_config == Path.AXIS_CONFIG_DELTA 
            and self.get_magnitude() > self.split_size 
            and ("X" in self.axes or "Y" in self.axes))

    def get_magnitude(self):
        """ Returns the magnitde in XYZ dim """
        if not self.mag:
            if self.rounded_vec == None:
                logging.error("Cannot get magnitude of vector without knowing its length")
            self.mag = np.linalg.norm(self.vec[:3])
        return self.mag

    def get_segments(self):
        """ Returns split segments for delta or arcs """
        if self.movement == Path.G2 or self.movement == Path.G3:
            return self.get_arc_segments()
        return self.get_delta_segments()
        

    def get_delta_segments(self):
        """ A delta segment must be split into lengths of self.split_size (default 1 mm) """
        if not self.needs_splitting():
            return [self]

        num_segments = np.round(self.get_magnitude()/self.split_size)+1
        #logging.debug("Magnitude: "+str(self.get_magnitude()))
        #logging.debug("Split size: "+str(self.split_size))
        #logging.debug("Num segments: "+str(num_segments))
        vals = np.transpose([
                    np.linspace(
                        self.prev.ideal_end_pos[i], 
                        self.ideal_end_pos[i], 
                        num_segments
                        ) for i in xrange(Path.MAX_AXES)]) 
        vals = np.delete(vals, 0, axis=0)
        vec_segments = [dict(zip(Path.axes_zipped, list(val))) for val in vals]
        path_segments = []

        for index, segment in enumerate(vec_segments):
            path = AbsolutePath(segment, self.speed, self.accel, self.cancelable, self.use_bed_matrix, False) #
            if index is not 0:
                path.set_prev(path_segments[-1])
            else:
                path.set_prev(self.prev)
            path_segments.append(path)

        return path_segments


    def parametric_circle(self, t, xc, yc, R):
        x = xc + R*np.cos(t)
        y = yc + R*np.sin(t)
        return x,y

    def inv_parametric_circle(self, x, xc, R):
        t = np.arccos((x-xc)/R)
        return t
        

    def get_arc_segments(self):
        # The code in this function was taken from 
        # http://stackoverflow.com/questions/11331854/how-can-i-generate-an-arc-in-numpy
        start_point = self.prev.ideal_end_pos[:2]
        end_point   = self.ideal_end_pos[:2]

        i = self.I
        j = self.J

        # Find radius
        R = np.sqrt(i**2 + j**2)

        logging.info(start_point)
        logging.info(end_point)
        logging.info(R)


        # Find start and end points
        start_t = self.inv_parametric_circle(start_point[0], start_point[0]+i, R)
        end_t   = self.inv_parametric_circle(end_point[0], start_point[0]+i, R)

        num_segments = np.ceil(np.abs(end_t-start_t)/self.split_size)+1


        # TODO: test this, it is probably wrong. 
        if self.movement == G2: 
            arc_T = np.linspace(start_t, end_t, num_segments)
        else:        
            arc_T = np.linspace(end_t, start_t, num_segments)
        X,Y = self.parametric_circle(arc_T, start_point[0]+i, start_point[1]+j, R)
    
        logging.info([X, Y])
        
        # Interpolate the remaining values
        vals = np.transpose([
                    np.linspace(
                        self.prev.ideal_end_pos[i], 
                        self.ideal_end_pos[i], 
                        num_segments
                        ) for i in xrange(Path.MAX_AXES)]) 

        # Update the X and Y positions
        for i, val in enumerate(vals):
            val[:2] = (X[i], Y[i])
        vals = np.delete(vals, 0, axis=0)

        vec_segments = [dict(zip(Path.axes_zipped, list(val))) for val in vals]
        path_segments = []

        for index, segment in enumerate(vec_segments):
            #print segment
            path = AbsolutePath(segment, self.speed, self.accel, self.cancelable, self.use_bed_matrix, False) #
            if index is not 0:
                path.set_prev(path_segments[-1])
            else:
                path.set_prev(self.prev)
            path_segments.append(path)

        #for seg in path_segments:
        #    logging.info(seg)
 

        return path_segments


    def set_prev_common(self, prev):

        # Cap the end position based on soft end stops
        if self.enable_soft_endstops:
            self.ideal_end_pos = np.clip(self.ideal_end_pos, Path.soft_min, Path.soft_max)

        # Calculate the position to reach, with bed levelling    
        self.level_end_pos = np.copy(self.ideal_end_pos)
        if self.use_bed_matrix:    
            self.level_end_pos[:3] = np.dot(Path.matrix_bed_comp, self.ideal_end_pos[:3])

        # Update the vector to move us from where we are, 
        # to where we ideally want to be. 
        self.vec = self.level_end_pos - self.start_pos

        # Compute stepper translation, 
        # yielding the discrete/rounded distance.
        vec = self.transform_vector(self.vec, self.start_pos)
        self.num_steps = np.round(np.abs(vec) * Path.steps_pr_meter)
        self.delta = np.sign(vec) * self.num_steps / Path.steps_pr_meter
        vec = self.reverse_transform_vector(self.delta, self.start_pos)

        # Vec now contains the actual distance we travelled. 

        # Calculate compensation
        self.backlash_compensate()

        # Set stepper and true posisional distance that was travelled, 
        # and can update the new end position.
        self.end_pos = self.start_pos + vec
        self.stepper_end_pos = self.start_pos + self.delta
        self.rounded_vec = vec

        #logging.debug("Ideal pos: "+str(self.ideal_end_pos[:3]))
        #logging.debug("Level pos: "+str(self.level_end_pos[:3]))
        #logging.debug("End   pos: "+str(self.end_pos[:3]))

        self.handle_tools()

        # Fix slave mode, if any
        self.handle_slaves()

        if np.isnan(vec).any():
            self.end_pos = self.start_pos
            self.num_steps = np.zeros(Path.MAX_AXES)
            self.delta = np.zeros(Path.MAX_AXES)

    def __str__(self):
        """ The vector representation of this path segment """
        return "Path from " + str(self.start_pos) + " to " + str(self.end_pos)

    @staticmethod
    def axis_to_index(axis):
        return Path.AXES.index(axis)

    @staticmethod
    def index_to_axis(index):
        return Path.AXES[index]

    @staticmethod
    def update_autolevel_matrix(probe_points, probe_heights):
        #TODO: Fix probe offset
        #offset_x = self.printer.config.getfloat('Probe', 'offset_x')
        #offset_y = self.printer.config.getfloat('Probe', 'offset_y')
        #offset_z = 0
        #offsets = {"X": offset_x, "Y": offset_y, "Z": offset_z}
        
        #measure_points = {key: probe_points[key] - offsets[key] for key in probe_points.keys()}

        mat = BedCompensation.create_rotation_matrix(probe_points, probe_heights)
        Path.matrix_bed_comp = mat
        Path.matrix_bed_comp_inv = np.linalg.inv(Path.matrix_bed_comp)

class AbsolutePath(Path):
    """ A path segment with absolute movement """
    def __init__(self, axes, speed, accel, cancelable=False, use_bed_matrix=True, use_backlash_compensation=True, enable_soft_endstops=True):
        Path.__init__(self, axes, speed, accel, cancelable, use_bed_matrix, use_backlash_compensation, enable_soft_endstops)
        self.movement = Path.ABSOLUTE

    def set_prev(self, prev):
        """ Set the previous path element """
        self.prev = prev
        prev.next = self
        self.start_pos = prev.end_pos

        # Make the start, end and path vectors. 
        self.end_pos = np.copy(self.start_pos)
        self.ideal_end_pos = np.copy(prev.ideal_end_pos)
        for index, axis in enumerate(Path.AXES):
            if axis in self.axes:
                self.ideal_end_pos[index] = self.axes[axis]

        self.set_prev_common(prev)


class RelativePath(Path):
    """ A path segment with Relative movement """
    def __init__(self, axes, speed, accel, cancelable=False, use_bed_matrix=True, use_backlash_compensation=True, enable_soft_endstops=True):
        Path.__init__(self, axes, speed, accel, cancelable, use_bed_matrix, use_backlash_compensation, enable_soft_endstops)
        self.movement = Path.RELATIVE

    def set_prev(self, prev):
        """ Link to previous segment """
        self.prev = prev
        prev.next = self
        self.start_pos = prev.end_pos

        # Generate the vector
        vec = np.zeros(Path.MAX_AXES, dtype=Path.DTYPE)
        for index, axis in enumerate(Path.AXES):
            if axis in self.axes:
                vec[index] = self.axes[axis]

        # Calculate the ideal end position. 
        # In an ideal world, this is where we want to go. 
        self.ideal_end_pos = prev.ideal_end_pos + vec

        self.set_prev_common(prev)

class G92Path(Path):
    """ A reset axes path segment. No movement occurs, only global position
    setting """
    def __init__(self, axes, cancelable=False):
        Path.__init__(self, axes, 0, 0)
        self.movement = Path.G92

    def set_prev(self, prev):
        """ Set the previous segment """
        self.prev = prev
        if prev is not None:
            self.start_pos = prev.end_pos
            self.ideal_end_pos = np.copy(prev.ideal_end_pos)
            prev.next = self
        else:
            self.start_pos = np.zeros(Path.MAX_AXES, dtype=Path.DTYPE)
            self.ideal_end_pos = np.copy(self.start_pos)

        self.end_pos = np.copy(self.start_pos)
        for index, axis in enumerate(Path.AXES):
            if axis in self.axes:
                self.end_pos[index] = self.ideal_end_pos[index] = self.axes[axis]
        self.vec = np.zeros(Path.MAX_AXES)
        self.rounded_vec = self.vec




# Simple test procedure for G2
if __name__ == '__main__':
    import numpy as np
    import os

    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                        datefmt='%m-%d %H:%M')

    Path.set_axes(5)
    Path.steps_pr_meter = np.ones(5)*10000
    g92 = G92Path({})
    g92.set_prev(None)

    p0 = RelativePath({"Y": 0.01}, 1, 1) 
    p0.set_prev(g92)

    p = RelativePath({"X": 0.01}, 1, 1)
    p.set_prev(p0)
    for seg in p.get_arc_segments(0.1, 0.1):
        print seg
    

