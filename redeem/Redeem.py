#!/usr/bin/python
"""
Redeem main program. This should run on the BeagleBone.

Minor version tag is Arnold Schwarzenegger movies chronologically.

Author: Elias Bakken
email: elias(at)iagent(dot)no
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

import glob
import shutil
import logging
import logging.handlers
import os
import os.path
import signal
from threading import Thread
from multiprocessing import JoinableQueue
import Queue
import numpy as np
import sys

from Mosfet import Mosfet
from Stepper import *
from Thermistor import Thermistor
from Fan import Fan
from Servo import Servo
from EndStop import EndStop
from USB import USB
from Pipe import Pipe
from Ethernet import Ethernet
from Extruder import Extruder, HBP
from Cooler import Cooler
from Path import Path
from PathPlanner import PathPlanner
from ColdEnd import ColdEnd
from PruFirmware import PruFirmware
from CascadingConfigParser import CascadingConfigParser
from Printer import Printer
from GCodeProcessor import GCodeProcessor
from PluginsController import PluginsController
from Delta import Delta
from Enable import Enable
from PWM import PWM
from RotaryEncoder import *
from FilamentSensor import *
from Alarm import Alarm, AlarmExecutor
from StepperWatchdog import StepperWatchdog
from Key_pin import Key_pin, Key_pin_listener
from Watchdog import Watchdog

# Global vars
printer = None

# Default logging level is set to debug
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                    datefmt='%m-%d %H:%M')
class Redeem:
    def __init__(self):
        firmware_version = "1.1.8~Raw Deal"
        logging.info("Redeem initializing "+firmware_version)

        printer = Printer()
        self.printer = printer
        Path.printer = printer

        printer.firmware_version = firmware_version

        # check for config files
        if not os.path.exists("/etc/redeem/default.cfg"):
            logging.error("/etc/redeem/default.cfg does not exist, this file is required for operation")
            sys.exit() # maybe use something more graceful?
            
        if not os.path.exists("/etc/redeem/local.cfg"):
            logging.info("/etc/redeem/local.cfg does not exist, Creating one")
            os.mknod("/etc/redeem/local.cfg")
    
        # Parse the config files.
        printer.config = CascadingConfigParser(
            ['/etc/redeem/default.cfg', '/etc/redeem/printer.cfg',
             '/etc/redeem/local.cfg'])

        # Get the revision and loglevel from the Config file
        level = self.printer.config.getint('System', 'loglevel')
        if level > 0:
            logging.getLogger().setLevel(level)

        # Set up additional logging, if present:        
        if self.printer.config.getboolean('System', 'log_to_file'):
            logfile = self.printer.config.get('System', 'logfile')
            formatter = '%(asctime)s %(name)-12s %(levelname)-8s %(message)s'
            printer.redeem_logging_handler = logging.handlers.RotatingFileHandler(logfile, maxBytes=2*1024*1024)
            printer.redeem_logging_handler.setFormatter(logging.Formatter(formatter))
            printer.redeem_logging_handler.setLevel(level)
            logging.getLogger().addHandler(printer.redeem_logging_handler)
            logging.info("-- Logfile configured --")

        # Find out which capes are connected
        self.printer.config.parse_capes()
        self.revision = self.printer.config.replicape_revision
        if self.revision:
            logging.info("Found Replicape rev. " + self.revision)
        else:
            logging.warning("Oh no! No Replicape present!")
            self.revision = "00B3"
        # We set it to 5 axis by default
        Path.NUM_AXES = 5
        if self.printer.config.reach_revision:
            logging.info("Found Reach rev. "+self.printer.config.reach_revision)
        if self.printer.config.reach_revision == "00A0":
            Path.NUM_AXES = 8
        elif self.printer.config.reach_revision == "00B0":
            Path.NUM_AXES = 7

        if self.revision in ["00A4", "0A4A", "00A3"]:
            PWM.set_frequency(100)
        elif self.revision in ["00B1", "00B2", "00B3"]:
            PWM.set_frequency(1000)

        # Test the alarm framework
        Alarm.printer = self.printer
        Alarm.executor = AlarmExecutor()
        alarm = Alarm(Alarm.ALARM_TEST, "Alarm framework operational")

        # Init the Watchdog timer
        printer.watchdog = Watchdog()

        # Enable PWM and steppers
        printer.enable = Enable("P9_41")
        printer.enable.set_disabled()

        # Init the Paths
        Path.axis_config = printer.config.getint('Geometry', 'axis_config')

        # Init the end stops
        EndStop.inputdev = self.printer.config.get("Endstops", "inputdev")
        # Set up key listener
        Key_pin.listener = Key_pin_listener(EndStop.inputdev)
        
        for es in ["Z2", "Y2", "X2", "Z1", "Y1", "X1"]: # Order matches end stop inversion mask in Firmware
            pin = self.printer.config.get("Endstops", "pin_"+es)
            keycode = self.printer.config.getint("Endstops", "keycode_"+es)
            invert = self.printer.config.getboolean("Endstops", "invert_"+es)
            self.printer.end_stops[es] = EndStop(printer, pin, keycode, es, invert)
            self.printer.end_stops[es].stops = self.printer.config.get('Endstops', 'end_stop_'+es+'_stops')

        # Init the 5 Stepper motors (step, dir, fault, DAC channel, name)
        if self.revision == "00A3":
            printer.steppers["X"] = Stepper_00A3("GPIO0_27", "GPIO1_29", "GPIO2_4" , 0, "X")
            printer.steppers["Y"] = Stepper_00A3("GPIO1_12", "GPIO0_22", "GPIO2_5" , 1, "Y")
            printer.steppers["Z"] = Stepper_00A3("GPIO0_23", "GPIO0_26", "GPIO0_15", 2, "Z")
            printer.steppers["E"] = Stepper_00A3("GPIO1_28", "GPIO1_15", "GPIO2_1" , 3, "E")
            printer.steppers["H"] = Stepper_00A3("GPIO1_13", "GPIO1_14", "GPIO2_3" , 4, "H")
        elif self.revision == "00B1":
            printer.steppers["X"] = Stepper_00B1("GPIO0_27", "GPIO1_29", "GPIO2_4" , 11, 0, "X")
            printer.steppers["Y"] = Stepper_00B1("GPIO1_12", "GPIO0_22", "GPIO2_5" , 12, 1, "Y")
            printer.steppers["Z"] = Stepper_00B1("GPIO0_23", "GPIO0_26", "GPIO0_15", 13, 2, "Z")
            printer.steppers["E"] = Stepper_00B1("GPIO1_28", "GPIO1_15", "GPIO2_1" , 14, 3, "E")
            printer.steppers["H"] = Stepper_00B1("GPIO1_13", "GPIO1_14", "GPIO2_3" , 15, 4, "H")
        elif self.revision == "00B2":
            printer.steppers["X"] = Stepper_00B2("GPIO0_27", "GPIO1_29", "GPIO2_4" , 11, 0, "X")
            printer.steppers["Y"] = Stepper_00B2("GPIO1_12", "GPIO0_22", "GPIO2_5" , 12, 1, "Y")
            printer.steppers["Z"] = Stepper_00B2("GPIO0_23", "GPIO0_26", "GPIO0_15", 13, 2, "Z")
            printer.steppers["E"] = Stepper_00B2("GPIO1_28", "GPIO1_15", "GPIO2_1" , 14, 3, "E")
            printer.steppers["H"] = Stepper_00B2("GPIO1_13", "GPIO1_14", "GPIO2_3" , 15, 4, "H")
        elif self.revision == "00B3":
            printer.steppers["X"] = Stepper_00B3("GPIO0_27", "GPIO1_29", 90, 11, 0, "X")
            printer.steppers["Y"] = Stepper_00B3("GPIO1_12", "GPIO0_22", 91, 12, 1, "Y")
            printer.steppers["Z"] = Stepper_00B3("GPIO0_23", "GPIO0_26", 92, 13, 2, "Z")
            printer.steppers["E"] = Stepper_00B3("GPIO1_28", "GPIO1_15", 93, 14, 3, "E")
            printer.steppers["H"] = Stepper_00B3("GPIO1_13", "GPIO1_14", 94, 15, 4, "H")
        elif self.revision in ["00A4", "0A4A"]:
            printer.steppers["X"] = Stepper_00A4("GPIO0_27", "GPIO1_29", "GPIO2_4" , 0, 0, "X")
            printer.steppers["Y"] = Stepper_00A4("GPIO1_12", "GPIO0_22", "GPIO2_5" , 1, 1, "Y")
            printer.steppers["Z"] = Stepper_00A4("GPIO0_23", "GPIO0_26", "GPIO0_15", 2, 2, "Z")
            printer.steppers["E"] = Stepper_00A4("GPIO1_28", "GPIO1_15", "GPIO2_1" , 3, 3, "E")
            printer.steppers["H"] = Stepper_00A4("GPIO1_13", "GPIO1_14", "GPIO2_3" , 4, 4, "H")
        # Init Reach steppers, if present. 
        if printer.config.reach_revision == "00A0":
            printer.steppers["A"] = Stepper_reach_00A4("GPIO2_2" , "GPIO1_18", "GPIO0_14", 5, 5, "A")
            printer.steppers["B"] = Stepper_reach_00A4("GPIO1_16", "GPIO0_5" , "GPIO0_14", 6, 6, "B")
            printer.steppers["C"] = Stepper_reach_00A4("GPIO0_3" , "GPIO3_19", "GPIO0_14", 7, 7, "C")
        elif printer.config.reach_revision == "00B0":
            printer.steppers["A"] = Stepper_reach_00B0("GPIO1_16", "GPIO0_5",  "GPIO0_3", 5, 5, "A")
            printer.steppers["B"] = Stepper_reach_00B0("GPIO2_2" , "GPIO0_14", "GPIO0_3", 6, 6, "B")


        # Enable the steppers and set the current, steps pr mm and
        # microstepping
        for name, stepper in self.printer.steppers.iteritems():
            stepper.in_use = printer.config.getboolean('Steppers', 'in_use_' + name)
            stepper.direction = printer.config.getint('Steppers', 'direction_' + name)
            stepper.has_endstop = printer.config.getboolean('Endstops', 'has_' + name)
            stepper.set_current_value(printer.config.getfloat('Steppers', 'current_' + name))
            stepper.set_steps_pr_mm(printer.config.getfloat('Steppers', 'steps_pr_mm_' + name))
            stepper.set_microstepping(printer.config.getint('Steppers', 'microstepping_' + name))
            stepper.set_decay(printer.config.getint("Steppers", "slow_decay_" + name))
            # Add soft end stops
            Path.soft_min[Path.axis_to_index(name)] = printer.config.getfloat('Endstops', 'soft_end_stop_min_' + name)
            Path.soft_max[Path.axis_to_index(name)] = printer.config.getfloat('Endstops', 'soft_end_stop_max_' + name)
            slave = printer.config.get('Steppers', 'slave_' + name)
            if slave:
                Path.add_slave(name, slave)
                logging.debug("Axis "+name+" has slave "+slave)

        # Commit changes for the Steppers
        #Stepper.commit()

        Stepper.printer = printer

        # Delta printer setup
        if Path.axis_config == Path.AXIS_CONFIG_DELTA:
            opts = ["Hez", "L", "r", "Ae", "Be", "Ce", "A_radial", "B_radial", "C_radial", "A_tangential", "B_tangential", "C_tangential" ]
            for opt in opts:
                Delta.__dict__[opt] = printer.config.getfloat('Delta', opt)

            Delta.recalculate()

        # Discover and add all DS18B20 cold ends.
        import glob
        paths = glob.glob("/sys/bus/w1/devices/28-*/w1_slave")
        logging.debug("Found cold ends: "+str(paths))
        for i, path in enumerate(paths):
            self.printer.cold_ends.append(ColdEnd(path, "ds18b20-"+str(i)))
            logging.info("Found Cold end "+str(i)+" on " + path)

        # Make Mosfets, thermistors and extruders
        heaters = ["E", "H", "HBP"]
        if self.printer.config.reach_revision:
            heaters.extend(["A", "B", "C"])
        for e in heaters:
            # Mosfets
            channel = self.printer.config.getint("Heaters", "mosfet_"+e)
            self.printer.mosfets[e] = Mosfet(channel)
            # Thermistors
            adc = self.printer.config.get("Heaters", "path_adc_"+e)
            chart = self.printer.config.get("Heaters", "temp_chart_"+e)
            resistance = self.printer.config.getfloat("Heaters", "resistance_"+e)
            self.printer.thermistors[e] = Thermistor(adc, "MOSFET "+e, chart, resistance)
            self.printer.thermistors[e].printer = printer

            # Extruders
            onoff = self.printer.config.getboolean('Heaters', 'onoff_'+e)
            prefix =  self.printer.config.get('Heaters', 'prefix_'+e)
            if e != "HBP":
                self.printer.heaters[e] = Extruder(
                                        self.printer.steppers[e],
                                        self.printer.thermistors[e], 
                                        self.printer.mosfets[e], e, onoff)
            else:
                self.printer.heaters[e] = HBP(
                                        self.printer.thermistors[e],
                                        self.printer.mosfets[e], onoff)
            self.printer.heaters[e].prefix = prefix
            self.printer.heaters[e].P = self.printer.config.getfloat('Heaters', 'pid_p_'+e)
            self.printer.heaters[e].I = self.printer.config.getfloat('Heaters', 'pid_i_'+e)
            self.printer.heaters[e].D = self.printer.config.getfloat('Heaters', 'pid_d_'+e)

            # Min/max settings
            self.printer.heaters[e].min_temp        = self.printer.config.getfloat('Heaters', 'min_temp_'+e)
            self.printer.heaters[e].max_temp        = self.printer.config.getfloat('Heaters', 'max_temp_'+e)
            self.printer.heaters[e].max_temp_rise   = self.printer.config.getfloat('Heaters', 'max_rise_temp_'+e)
            self.printer.heaters[e].max_temp_fall   = self.printer.config.getfloat('Heaters', 'max_fall_temp_'+e)

        # Init the three fans. Argument is PWM channel number
        self.printer.fans = []
        if self.revision == "00A3":
            self.printer.fans.append(Fan(0))
            self.printer.fans.append(Fan(1))
            self.printer.fans.append(Fan(2))
        elif self.revision == "0A4A":
            self.printer.fans.append(Fan(8))
            self.printer.fans.append(Fan(9))
            self.printer.fans.append(Fan(10))
        elif self.revision in ["00B1", "00B2", "00B3"]:
            self.printer.fans.append(Fan(7))
            self.printer.fans.append(Fan(8))
            self.printer.fans.append(Fan(9))
            self.printer.fans.append(Fan(10))
        if printer.config.reach_revision == "00A0":
            self.printer.fans.append(Fan(14))
            self.printer.fans.append(Fan(15))
            self.printer.fans.append(Fan(7))

        # Disable all fans
        for f in self.printer.fans:
            f.set_value(0)

        # Init the servos
        printer.servos = []
        servo_nr = 0
        while(printer.config.has_option("Servos", "servo_"+str(servo_nr)+"_enable")):
            if printer.config.getboolean("Servos", "servo_"+str(servo_nr)+"_enable"):
                channel = printer.config.get("Servos", "servo_"+str(servo_nr)+"_channel")
                pulse_min = printer.config.getfloat("Servos", "servo_"+str(servo_nr)+"_pulse_min")
                pulse_max = printer.config.getfloat("Servos", "servo_"+str(servo_nr)+"_pulse_max")
                angle_min = printer.config.getfloat("Servos", "servo_"+str(servo_nr)+"_angle_min")
                angle_max = printer.config.getfloat("Servos", "servo_"+str(servo_nr)+"_angle_max")
                angle_init = printer.config.getfloat("Servos", "servo_"+str(servo_nr)+"_angle_init")
                s = Servo(channel, pulse_min, pulse_max, angle_min, angle_max, angle_init)
                printer.servos.append(s)
                logging.info("Added servo "+str(servo_nr))
            servo_nr += 1

        # Connect thermitors to fans
        for t, therm in self.printer.heaters.iteritems():
            for f, fan in enumerate(self.printer.fans):
                if not self.printer.config.has_option('Cold-ends', "connect-therm-{}-fan-{}".format(t, f)):
                    continue
                if printer.config.getboolean('Cold-ends', "connect-therm-{}-fan-{}".format(t, f)):
                    c = Cooler(therm, fan, "Cooler-{}-{}".format(t, f), True) # Use ON/OFF on these. 
                    c.ok_range = 4
                    opt_temp = "therm-{}-fan-{}-target_temp".format(t, f)
                    if printer.config.has_option('Cold-ends', opt_temp): 
                        target_temp = printer.config.getfloat('Cold-ends', opt_temp)
                    else:            
                        target_temp = 60    
                    c.set_target_temperature(target_temp)
                    c.enable()
                    printer.coolers.append(c)
                    logging.info("Cooler connects therm {} with fan {}".format(t, f))

        # Connect fans to M106
        printer.controlled_fans = []
        for i, fan in enumerate(self.printer.fans):
            if not self.printer.config.has_option('Cold-ends', "add-fan-{}-to-M106".format(i)):
                continue
            if self.printer.config.getboolean('Cold-ends', "add-fan-{}-to-M106".format(i)):
                printer.controlled_fans.append(self.printer.fans[i])
                logging.info("Added fan {} to M106/M107".format(i))

        # Connect the colds to fans
        for ce, cold_end in enumerate(self.printer.cold_ends):
            for f, fan in enumerate(self.printer.fans):
                option = "connect-ds18b20-{}-fan-{}".format(ce, f)
                if self.printer.config.has_option('Cold-ends', option):
                    if self.printer.config.getboolean('Cold-ends', option):
                        c = Cooler(cold_end, fan, "Cooler-ds18b20-{}-{}".format(ce, f), False)
                        c.ok_range = 4
                        opt_temp = "cooler_{}_target_temp".format(ce)
                        if printer.config.has_option('Cold-ends', opt_temp): 
                            target_temp = printer.config.getfloat('Cold-ends', opt_temp)
                        else:            
                            target_temp = 60    
                        c.set_target_temperature(target_temp)
                        c.enable()
                        printer.coolers.append(c)
                        logging.info("Cooler connects temp sensor ds18b20 {} with fan {}".format(ce, f))

        # Init roatray encs. 
        printer.filament_sensors = []

        # Init rotary encoders
        printer.rotary_encoders = []
        for ex in ["E", "H", "A", "B", "C"]:
            if not printer.config.has_option('Rotary-encoders', "enable-{}".format(ex)):
                continue
            if printer.config.getboolean("Rotary-encoders", "enable-{}".format(ex)):
                logging.debug("Rotary encoder {} enabled".format(ex))
                event = printer.config.get("Rotary-encoders", "event-{}".format(ex))
                cpr = printer.config.getint("Rotary-encoders", "cpr-{}".format(ex))
                diameter = printer.config.getfloat("Rotary-encoders", "diameter-{}".format(ex))
                r = RotaryEncoder(event, cpr, diameter)
                printer.rotary_encoders.append(r)
                # Append as Filament Sensor
                ext_nr = Path.axis_to_index(ex)-3
                sensor = FilamentSensor(ex, r, ext_nr, printer)
                alarm_level = printer.config.getfloat("Filament-sensors", "alarm-level-{}".format(ex))
                logging.debug("Alarm level"+str(alarm_level))
                sensor.alarm_level = alarm_level
                printer.filament_sensors.append(sensor)
    
        # Make a queue of commands
        self.printer.commands = JoinableQueue(10)

        # Make a queue of commands that should not be buffered
        self.printer.sync_commands = JoinableQueue()
        self.printer.unbuffered_commands = JoinableQueue(10)

        # Bed compensation matrix
        Path.matrix_bed_comp = printer.load_bed_compensation_matrix()
        Path.matrix_bed_comp_inv = np.linalg.inv(Path.matrix_bed_comp)
        logging.debug("Loaded bed compensation matrix: \n"+str(Path.matrix_bed_comp))

        for axis in printer.steppers.keys():
            i = Path.axis_to_index(axis)
            Path.max_speeds[i] = printer.config.getfloat('Planner', 'max_speed_'+axis.lower())
            Path.min_speeds[i] = printer.config.getfloat('Planner', 'min_speed_'+axis.lower())
            Path.jerks[i] = printer.config.getfloat('Planner', 'max_jerk_'+axis.lower())
            Path.home_speed[i] = printer.config.getfloat('Homing', 'home_speed_'+axis.lower())
            Path.home_backoff_speed[i] = printer.config.getfloat('Homing', 'home_backoff_speed_'+axis.lower())
            Path.home_backoff_offset[i] = printer.config.getfloat('Homing', 'home_backoff_offset_'+axis.lower())
            Path.steps_pr_meter[i] = printer.steppers[axis].get_steps_pr_meter()
            Path.backlash_compensation[i] = printer.config.getfloat('Steppers', 'backlash_'+axis.lower())

        dirname = os.path.dirname(os.path.realpath(__file__))

        # Create the firmware compiler
        pru_firmware = PruFirmware(
            dirname + "/firmware/firmware_runtime.p",
            dirname + "/firmware/firmware_runtime.bin",
            dirname + "/firmware/firmware_endstops.p",
            dirname + "/firmware/firmware_endstops.bin",
            self.printer, "/usr/bin/pasm")

        
        printer.move_cache_size = printer.config.getfloat('Planner', 'move_cache_size')
        printer.print_move_buffer_wait = printer.config.getfloat('Planner', 'print_move_buffer_wait')
        printer.min_buffered_move_time = printer.config.getfloat('Planner', 'min_buffered_move_time')
        printer.max_buffered_move_time = printer.config.getfloat('Planner', 'max_buffered_move_time')

        self.printer.processor = GCodeProcessor(self.printer)
        self.printer.plugins = PluginsController(self.printer)

        # Path planner
        travel_default = False
        center_default = False
        home_default = False

        # Setting acceleration before PathPlanner init
        for axis in printer.steppers.keys():
            Path.acceleration[Path.axis_to_index(axis)] = printer.config.getfloat(
                                                        'Planner', 'acceleration_' + axis.lower())

        self.printer.path_planner = PathPlanner(self.printer, pru_firmware)
        for axis in printer.steppers.keys():
            i = Path.axis_to_index(axis)
            
            # Sometimes soft_end_stop aren't defined to be at the exact hardware boundary.
            # Adding 100mm for searching buffer.
            if printer.config.has_option('Geometry', 'travel_' + axis.lower()):
                printer.path_planner.travel_length[axis] = printer.config.getfloat('Geometry', 'travel_' + axis.lower())
            else:
                printer.path_planner.travel_length[axis] = (Path.soft_max[i] - Path.soft_min[i]) + .1
                if axis in ['X','Y','Z']:                
                    travel_default = True
            
            if printer.config.has_option('Geometry', 'offset_' + axis.lower()):
                printer.path_planner.center_offset[axis] = printer.config.getfloat('Geometry', 'offset_' + axis.lower())
            else:
                printer.path_planner.center_offset[axis] =(Path.soft_min[i] if Path.home_speed[i] > 0 else Path.soft_max[i])
                if axis in ['X','Y','Z']:                
                    center_default = True

            if printer.config.has_option('Homing', 'home_' + axis.lower()):
                printer.path_planner.home_pos[axis] = printer.config.getfloat('Homing', 'home_' + axis.lower())
            else:
                printer.path_planner.home_pos[axis] = printer.path_planner.center_offset[axis]
                if axis in ['X','Y','Z']:                   
                    home_default = True
                
        if Path.axis_config == Path.AXIS_CONFIG_DELTA:
            if travel_default:
                logging.warning("Axis travel (travel_*) set by soft limits, manual setup is recommended for a delta")
            if center_default:
                logging.warning("Axis offsets (offset_*) set by soft limits, manual setup is recommended for a delta")
            if home_default:
                logging.warning("Home position (home_*) set by soft limits or offset_*")
                logging.info("Home position will be recalculated...")
        
                # convert home_pos to effector space
                Az = printer.path_planner.home_pos['X']
                Bz = printer.path_planner.home_pos['Y']
                Cz = printer.path_planner.home_pos['Z']
                
                z_offset = Delta.vertical_offset(Az,Bz,Cz) # vertical offset
                xyz = Delta.forward_kinematics2(Az, Bz, Cz) # effector position
                
                # The default home_pos, provided above, is based on effector space 
                # coordinates for carriage positions. We need to transform these to 
                # get where the effector actually is.
                xyz[2] += z_offset
                for i, a in enumerate(['X','Y','Z']):
                    printer.path_planner.home_pos[a] = xyz[i]
                
                logging.info("Home position = %s"%str(printer.path_planner.home_pos))

        # Enable Stepper timeout
        timeout = printer.config.getint('Steppers', 'timeout_seconds')
        printer.swd = StepperWatchdog(printer, timeout)
        if printer.config.getboolean('Steppers', 'use_timeout'):
            printer.swd.start()

        # Set up communication channels
        printer.comms["USB"] = USB(self.printer)
        printer.comms["Eth"] = Ethernet(self.printer)

        if Pipe.check_tty0tty() or Pipe.check_socat():
            printer.comms["octoprint"] = Pipe(printer, "octoprint")
            printer.comms["toggle"] = Pipe(printer, "toggle")
            printer.comms["testing"] = Pipe(printer, "testing")
            printer.comms["testing_noret"] = Pipe(printer, "testing_noret")
            # Does not send "ok"
            printer.comms["testing_noret"].send_response = False
        else:
            logging.warning("Neither tty0tty or socat is installed! No virtual tty pipes enabled")


    def start(self):
        """ Start the processes """
        self.running = True
        # Start the two processes
        p0 = Thread(target=self.loop,
                    args=(self.printer.commands, "buffered"))
        p1 = Thread(target=self.loop,
                    args=(self.printer.unbuffered_commands, "unbuffered"))
        p2 = Thread(target=self.eventloop,
                    args=(self.printer.sync_commands, "sync"))
        p0.daemon = True
        p1.daemon = True
        p2.daemon = True

        p0.start()
        p1.start()
        p2.start()

        Alarm.executor.start()
        Key_pin.listener.start()

        if self.printer.config.getboolean('Watchdog', 'enable_watchdog'):
            self.printer.watchdog.start()

        self.printer.enable.set_enabled()

        # Signal everything ready
        logging.info("Redeem ready")

    def loop(self, queue, name):
        """ When a new gcode comes in, execute it """
        try:
            while self.running:
                try:
                    gcode = queue.get(block=True, timeout=1)
                except Queue.Empty:
                    continue
                logging.debug("Executing "+gcode.code()+" from "+name + " " + gcode.message)
                self._execute(gcode)
                self.printer.reply(gcode)
                queue.task_done()
        except Exception:
            logging.exception("Exception in {} loop: ".format(name))

    def eventloop(self, queue, name):
        """ When a new event comes in, execute the pending gcode """
        try:
            while self.running:
                # Returns False on timeout, else True
                if self.printer.path_planner.wait_until_sync_event():
                    try:
                        gcode = queue.get(block=True, timeout=1)
                    except Queue.Empty:
                        continue
                    self._synchronize(gcode)
                    logging.info("Event handled for " + gcode.code() + " from " + name + " " + gcode.message)
                    queue.task_done()
        except Exception:
            logging.exception("Exception in {} eventloop: ".format(name))

    def exit(self):
        logging.info("Redeem starting exit")
        self.running = False
        self.printer.path_planner.wait_until_done()
        self.printer.path_planner.force_exit()

        # Stops plugins
        self.printer.plugins.exit()

        for name, stepper in self.printer.steppers.iteritems():
            stepper.set_disabled()
        Stepper.commit()

        for name, heater in self.printer.heaters.iteritems():
            logging.debug("closing "+name)
            heater.disable()

        for name, comm in self.printer.comms.iteritems():
            logging.debug("closing "+name)
            comm.close()
        self.printer.enable.set_disabled()
        self.printer.swd.stop()
        Alarm.executor.stop()
        Key_pin.listener.stop()
        self.printer.watchdog.stop()
        self.printer.enable.set_disabled()

        logging.info("Redeem exited")

    def _execute(self, g):
        """ Execute a G-code """
        if g.message == "ok" or g.code() == "ok" or g.code() == "No-Gcode":
            g.set_answer(None)
            return
        if g.is_info_command():
            desc = self.printer.processor.get_long_description(g)
            self.printer.send_message(g.prot, desc)
        else:
            self.printer.processor.execute(g)

    def _synchronize(self, g):
        """ Syncrhonized execution of a G-code """
        self.printer.processor.synchronize(g)



def main():
    # Create Redeem
    r = Redeem()

    def signal_handler(signal, frame):
        r.exit()

    # Register signal handler to allow interrupt with CTRL-C
    signal.signal(signal.SIGINT, signal_handler)

    # Launch Redeem
    r.start()

    # Wait for end of process signal
    signal.pause()



def profile():
    import yappi
    yappi.start()
    main()
    yappi.get_func_stats().print_all()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "profile":
        profile()
    else:
        main()
