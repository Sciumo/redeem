"""
GCode G30
Single Z probe

Author: Elias Bakken
email: elias(dot)bakken(at)gmail dot com
Website: http://www.thing-printer.com
License: CC BY-SA: http://creativecommons.org/licenses/by-sa/2.0/
"""

from GCodeCommand import GCodeCommand
import logging
try:
    from Gcode import Gcode
    from Path import G92Path
except ImportError:
    from redeem.Gcode import Gcode
    from redeem.Path import G92Path

class G30(GCodeCommand):

    def execute(self, g):
        if g.has_letter("P"): # Load point
            index = int(g.get_value_by_letter("P"))
            point = self.printer.probe_points[index]
        else:
            # If no porobe point is specified, use current pos
            point = self.printer.path_planner.get_current_pos()
        if g.has_letter("X"): # Override X
            point["X"] = float(g.get_value_by_letter("X"))
        if g.has_letter("Y"): # Override Y
            point["Y"] = float(g.get_value_by_letter("Y"))
        if g.has_letter("Z"): # Override Z
            point["Z"] = float(g.get_value_by_letter("Z"))        

        # Get probe length, if present, else use 1 cm. 
        if g.has_letter("D"):
            probe_length = float(g.get_value_by_letter("D"))
        else:
            probe_length = self.printer.config.getfloat('Probe', 'length')

        # Get probe speed. If not preset, use printers curent speed. 
        if g.has_letter("F"):
            probe_speed = float(g.get_value_by_letter("F")) / 60000.0
        else:
            probe_speed = self.printer.config.getfloat('Probe', 'length')
        
        # Get acceleration. If not present, use value from config.        
        if g.has_letter("A"):
            probe_accel = float(g.get_value_by_letter("A"))
        else:
            probe_accel = self.printer.config.getfloat('Probe', 'accel')
        
        # Find the Probe offset
        offset_x = self.printer.config.getfloat('Probe', 'offset_x')*1000
        offset_y = self.printer.config.getfloat('Probe', 'offset_y')*1000

        # Move to the position
        G0 = Gcode({"message": "G0 X{} Y{} Z{}".format(point["X"]+offset_x, point["Y"]+offset_y, point["Z"]), "prot": g.prot})    
        self.printer.processor.execute(G0)
        self.printer.path_planner.wait_until_done()
        bed_dist = self.printer.path_planner.probe(probe_length, probe_speed, probe_accel) # Probe one cm. TODO: get this from config
        logging.debug("Bed dist: "+str(bed_dist*1000)+" mm")

        # Add the probe offsets to the points
        
        #logging.info("Found Z probe height {} at (X, Y) = ({}, {})".format(bed_dist, point["X"], point["Y"]))
        if g.has_letter("S"):
            if not g.has_letter("P"):
                logging.warning("G30: S-parameter was set, but no index (P) was set.")
            else:
                self.printer.probe_heights[index] = bed_dist
                self.printer.send_message(g.prot, 
                    "Found Z probe height {} at (X, Y) = ({}, {})".format(bed_dist, point["X"], point["Y"]))
        

    def get_description(self):
        return "Probe the bed at current point"

    def get_long_description(self):
        return ("Probe the bed at the current position, or if specified, a point "
                "previously set by M557. X, Y, and Z starting probe positions can be overridden, "
                "D sets the probe length, or taken from config if nothing is specified. "
                "F sets the probe speed. If not present, it's taken from the config"
                "A sets the probe acceleration. If not present, it's taken from the config")
   
    def is_buffered(self):
        return True

    def get_test_gcodes(self):
        return ["G30", "G30 P0", "G30 P1 X10 Y10"]

