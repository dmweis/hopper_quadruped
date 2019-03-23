import rospy

from hopper_controller.msg import HexapodMotorPositions, LegMotorPositions
from .hexapod_ik_driver import LegFlags


def move_towards(target, current, step=0.4):
    if abs(target-current) < step:
        return target, True
    else:
        if target > current:
            return current + step, False
        else:
            return current - step, False


class FoldingManager(object):
    def __init__(self, body_controller):
        super(FoldingManager, self).__init__()
        self.body_controller = body_controller
        self.last_motor_position = None

    def unfold(self):
        current_position = self.body_controller.read_hexapod_motor_positions()
        self.last_motor_position = current_position
        # left side
        left_middle_backwards = current_position.left_middle.coxa > 150.0
        right_middle_backwards = current_position.right_middle.coxa < 150.0
        while True:
            rospy.sleep(0.01)
            self.last_motor_position.left_middle.coxa, left_done = move_towards(150, self.last_motor_position.left_middle.coxa)
            self.last_motor_position.right_middle.coxa, right_done = move_towards(150, self.last_motor_position.right_middle.coxa)
            self.body_controller.set_motors(self.last_motor_position)
            if left_done and right_done:
                break
        while True:
            rospy.sleep(0.01)
            self.last_motor_position.left_front.coxa, one = move_towards(150, self.last_motor_position.left_front.coxa)
            self.last_motor_position.right_front.coxa, two = move_towards(150, self.last_motor_position.right_front.coxa)

            self.last_motor_position.left_rear.coxa, three = move_towards(150, self.last_motor_position.left_rear.coxa)
            self.last_motor_position.right_rear.coxa, four = move_towards(150, self.last_motor_position.right_rear.coxa)
            self.body_controller.set_motors(self.last_motor_position)
            if one and two and three and four:
                break
        self.body_controller.set_torque(False)
        while True:
            rospy.logerr(self.body_controller.read_hexapod_motor_positions())

