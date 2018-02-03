#!/usr/bin/env python

from __future__ import absolute_import
import rospy
from geometry_msgs.msg import Twist
from hexapod_gait_engine import GaitController
from hexapod_ik_driver import IkDriver, Vector2
from dynamixel_driver import DynamixelDriver, search_usb_2_ax_port


class HexapodController(object):
    def __init__(self):
        servo_driver = DynamixelDriver(search_usb_2_ax_port())
        ik_driver = IkDriver(servo_driver)
        self.controller = GaitController(ik_driver)

    def update_direction(self, twist):
        self.controller.direction = Vector2(twist.linear.x, twist.linear.y)
        self.controller.rotation = twist.angular.x

    def listener(self):
        rospy.init_node('quadruped_controller', anonymous=True)
        rospy.Subscriber("quadruped_command", Twist, self.update_direction)
        rospy.spin()
        self.controller.stop()


if __name__ == '__main__':
    HexapodController().listener()