"""
Extruder file for Replicape.

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

from threading import Thread
import time
import logging
import numpy as np
from Alarm import Alarm

class Heater(object):
    """
    A heater element that must keep temperature,
    either an extruder, a HBP or could even be a heated chamber
    """
    def __init__(self, thermistor, mosfet, name, onoff_control):
        """ Init """
        self.thermistor = thermistor
        self.mosfet = mosfet
        self.name = name                   # Name, used for debugging
        self.current_temp = 0.0
        self.target_temp = 0.0             # Target temperature (Ts). Start off. 
        self.last_error = 0.0              # Previous error term, used in calculating the derivative
        self.error_integral = 0.0          # Accumulated integral since the temperature came within the boudry
        self.error_integral_limit = 100.0  # Integral temperature boundary
        self.P = 1.0                      # Proportional 
        self.I = 0.0                      # Integral 
        self.D = 0.0                      # Derivative
        self.onoff_control = onoff_control  # If we use PID or ON/OFF control
        self.ok_range = 4.0
        self.prefix = ""
        self.sleep = 0.1                 # Time to sleep between measurements

        self.min_temp_enabled   = False  # Temperature error limit 
        self.min_temp           = 0      # If temperature falls below this point from the target, disable. 
        self.max_temp           = 0      # Max temp that can be reached before disabling printer. 
        self.max_temp_rise      = 0      # Fastest temp can rise pr measrement
        self.max_temp_fall      = 0      # Fastest temp can fall pr measurement

        self.extruder_error = False

    def set_target_temperature(self, temp):
        """ Set the desired temperature of the extruder """
        self.min_temp_enabled = False
        self.target_temp = float(temp)

    def get_temperature(self):
        """ get the temperature of the thermistor"""
        return np.average(self.temperatures[-self.avg:])

    def get_target_temperature(self):
        """ get the temperature of the thermistor"""
        return self.target_temp

    def is_target_temperature_reached(self):
        """ Returns true if the target temperature is reached """
        if self.target_temp == 0:
            return True
        err = abs(self.current_temp - self.target_temp)
        reached = err < self.ok_range
        return reached

    def is_temperature_stable(self, seconds=10):
        """ Returns true if the temperature has been stable for n seconds """
        if len(self.temperatures) < int(seconds/self.sleep):
            return False
        if max(self.temperatures[-int(seconds/self.sleep):]) > (self.target_temp + self.ok_range):
            return False
        if min(self.temperatures[-int(seconds/self.sleep):]) < (self.target_temp - self.ok_range):
            return False
        return True

    def set_min_temp(self, min_temp):
        """ Set the minimum temperature. If current temp goes below this, 
        sound the alarm """
        self.current_min_temp = min_temp
    
    def enable_min_temp(self):
        """ Enable minimum temperature alarm """
        self.min_temp_enabled = True
        logging.info("Min temp alarm enabled at {} for {}".format(self.min_temp, self.name))
    
    def disable(self):
        """ Stops the heater and the PID controller """
        self.target_temp = 0
        self.enabled = False
        self.mosfet.set_power(0.0)
        # Wait for PID to stop
        self.t.join()
        logging.debug("Heater {} disabled".format(self.name))
        self.mosfet.set_power(0.0)
        self.last_error = 0.0
        self.error_integral = 0.0
        self.error_integral_limit = 100.0

    def enable(self):
        """ Start the PID controller """
        self.avg = max(int(1.0/self.sleep), 3)
        self.error = 0
        self.errors = [0]*self.avg
        self.average = 0
        self.averages = [0]*self.avg
        self.prev_time = self.current_time = time.time()
        self.temperatures = []  
        self.enabled = True
        self.t = Thread(target=self.keep_temperature, name=self.name)
        self.t.start()

    def keep_temperature(self):
        """ PID Thread that keeps the temperature stable """
        try:
            while self.enabled:
                self.current_temp = self.thermistor.get_temperature()
                self.temperatures.append(self.current_temp)
                self.temperatures[:-max(int(60/self.sleep), self.avg)] = [] # Keep only this much history

                self.error = self.target_temp-self.current_temp
                self.errors.append(self.error)
                self.errors.pop(0)
                self.average = sum(self.errors)/self.avg
                self.averages.append(self.average)
                self.averages.pop(0)

                if self.onoff_control:
                    if self.error > 1.0:
                        power = 1.0
                    else:
                        power = 0.0
                else:
                    derivative = self.get_error_derivative()
                    integral = self.get_error_integral()
                    if abs(self.error) > 20:  # Avoid windup
                        self.error_integral = 0
                        integral = 0
                    power = self.P*(self.average + self.D*derivative + self.I*integral)  # The standard formula for the PID
                    power = max(min(power, 1.0), 0.0)                           # Normalize to 0,1
                    #if self.name =="E":
                    #    logging.debug("Der: "+str(derivative)+" Err: "+str(self.error)+" avg err: "+str(self.average))

                # Run safety checks
                self.time_diff = self.current_time-self.prev_time
                self.prev_time = self.current_time
                self.current_time = time.time()

                if not self.extruder_error:
                    self.check_temperature_error()

                # Set temp if temperature is OK
                if not self.extruder_error:
                    self.mosfet.set_power(power)
                else:
                    self.mosfet.set_power(0)        
                time.sleep(self.sleep)
        finally:
            # Disable this mosfet if anything goes wrong
            self.mosfet.set_power(0)

    def get_error_derivative(self):
        """ Get the derivative of the error term """
        return (self.average-self.averages[-2])/self.sleep		# Calculate the diff

    def get_error_integral(self):
        """ Calculate and return the error integral """
        self.error_integral += self.error*self.sleep
        return self.error_integral

    def check_temperature_error(self):
        """ Check the temperatures, make sure they are sane. 
        Sound the alarm if something is wrong """
        if len(self.temperatures) < 2:
            return
        temp_delta = self.temperatures[-1]-self.temperatures[-2]
        # Check that temperature is not rising too quickly
        if temp_delta > self.max_temp_rise:
            a = Alarm(Alarm.HEATER_RISING_FAST, 
                "Temperature rising too quickly ({}) for {}".format(temp_delta, self.name))
        # Check that temperature is not falling too quickly
        if temp_delta < -self.max_temp_fall:
            a = Alarm(Alarm.HEATER_FALLING_FAST, 
                "Temperature falling too quickly ({}) for {}".format(temp_delta, self.name))
        # Check that temperature has not fallen below a certain setpoint from target
        if self.min_temp_enabled and self.current_temp < (self.target_temp - self.min_temp):
            a = Alarm(Alarm.HEATER_TOO_COLD, 
                "Temperature below min set point ({}) for {}".format(self.min_temp, self.name), 
                "Alarm: Heater {}".format(self.name))
        # Check if the temperature has gone beyond the max value
        if self.current_temp > self.max_temp:
            a = Alarm(Alarm.HEATER_TOO_HOT, 
                "Temperature beyond max ({}) for {}".format(self.max_temp, self.name))                
        # Check the time diff, only warn if something is off.     
        if self.time_diff > 2:
            logging.warning("Heater time update large: " +
                            self.name + " temp: " +
                            str(self.current_temp) + " time delta: " +
                            str(self.current_time-self.prev_time))



class Extruder(Heater):
    """ Subclass for Heater, this is an extruder """
    def __init__(self, smd, thermistor, mosfet, name, onoff_control):
        Heater.__init__(self, thermistor, mosfet, name, onoff_control)
        self.smd = smd
        self.sleep = 0.25
        self.enable()


class HBP(Heater):
    """ Subclass for heater, this is a Heated build platform """
    def __init__(self, thermistor, mosfet, onoff_control):
        Heater.__init__(self, thermistor, mosfet, "HBP", onoff_control)
        self.sleep = 0.5 # Heaters have more thermal mass
        self.enable()
