#!/usr/bin/env python

import rospy
from std_msgs.msg import String
from hopper_msgs.msg import ServoTelemetrics, HexapodTelemetrics

def mean(numbers):
    return float(sum(numbers)) / max(len(numbers), 1)

class BatteryStatusMonitor(object):
    def __init__(self):
        super(BatteryStatusMonitor, self).__init__()
        rospy.init_node("hopper_battery_monitor")
        self.lowest_recorded_voltage = 15.0
        self.speech_publisher = rospy.Publisher('hopper_play_sound', String, queue_size=5)
        # sleep for 30 seconds to prevent overcovering with init voice
        # TODO: find a better way to do this
        rospy.sleep(30)
        self.telemetrics_sub = rospy.Subscriber("hopper_telemetrics", HexapodTelemetrics, self.on_new_telemetrics, queue_size=1)
        rospy.spin()

    def on_new_telemetrics(self, message):
        voltages = [data.voltage for data in message.servos]
        mean_voltage = mean(voltages)
        if mean_voltage < self.lowest_recorded_voltage:
            if mean_voltage < 10.5 and self.lowest_recorded_voltage > 10.5:
                self.speech_publisher.publish("battery_critical")
            elif mean_voltage < 11 and self.lowest_recorded_voltage > 11:
                self.speech_publisher.publish("battery_below_11")
            elif mean_voltage < 12 and self.lowest_recorded_voltage > 12:
                self.speech_publisher.publish("battery_below_12")
            self.lowest_recorded_voltage = mean_voltage


if __name__ == '__main__':
    BatteryStatusMonitor()