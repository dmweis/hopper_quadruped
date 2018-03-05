from __future__ import division
from __future__ import absolute_import
from quadruped_ik_driver import LegPositions, IkDriver, Vector3, Vector2, LegFlags
import threading
from time import sleep
import math

INTERPOLATION_FREQUENCY = 30

LEG_HEIGHT = -9
LEG_DISTANCE_LONGITUDAL = 15
LEG_DISTANCE_LATERAL = 15

RELAXED_POSITION = LegPositions(
    Vector3(LEG_DISTANCE_LATERAL, LEG_DISTANCE_LONGITUDAL, LEG_HEIGHT),
    Vector3(LEG_DISTANCE_LATERAL, -LEG_DISTANCE_LONGITUDAL, LEG_HEIGHT),
    Vector3(-LEG_DISTANCE_LATERAL, LEG_DISTANCE_LONGITUDAL, LEG_HEIGHT),
    Vector3(-LEG_DISTANCE_LATERAL, -LEG_DISTANCE_LONGITUDAL, LEG_HEIGHT),
)


def get_height_for_step(distance, full_step_lenght, height):
    distance = distance / full_step_lenght
    return math.sin(distance * math.pi) * height


class GaitController(threading.Thread):
    def __init__(self, ik_driver):
        super(GaitController, self).__init__()
        self.ik_driver = ik_driver
        self.direction = Vector2(0, 0)
        self.rotation = 0
        self.__last_written_position = RELAXED_POSITION.clone()
        self.__last_used_forward_legs = LegFlags.RF_LR_CROSS
        # self.__last_used_forward_legs = LegFlags.LF_RR_CROSS
        self.__speed = 1
        self.__update_delay = 1000 / 30
        self.__keep_running = True
        self.ik_driver.setup()
        self.start()

    def run(self):
        while self.__keep_running:
            if not self.direction.is_zero() or self.rotation != 0:
                self.__execute_step(self.direction, self.rotation, self.__get_next_leg_combo())
            else:
                sleep(self.__update_delay * 0.001)

    def stop(self):
        self.__keep_running = False
        self.join()
        self.ik_driver.disable_motors()
        self.ik_driver.close()

    def __execute_step(self, direction, angle, forward_legs, leg_lift_height = 2):
        backwards_legs = LegFlags.RF_LR_CROSS if forward_legs == LegFlags.LF_RR_CROSS else LegFlags.LF_RR_CROSS
        start_position = self.__last_written_position.clone()
        target_position = RELAXED_POSITION.clone() \
            .transform(Vector3(direction.x / 2, direction.y / 2, 0), forward_legs) \
            .transform(Vector3(-direction.x / 2, -direction.y / 2, 0), backwards_legs)
        transformation_vectors = target_position - start_position
        normalized_transformation_vectors = transformation_vectors.clone()
        normalized_transformation_vectors.normalize_vectors()
        total_distance = transformation_vectors.left_front.length()
        distance_traveled = 0
        print "Starting step " + str(forward_legs)
        print "Start pos " + str(start_position)
        print "Target pos " + str(target_position)
        print "Transform pos " + str(transformation_vectors)
        print "Distance " + str(direction.length())
        print "Total Distance " + str(total_distance)
        while distance_traveled <= total_distance:
            distance_traveled += self.__speed / self.__update_delay
            new_position = start_position + normalized_transformation_vectors * distance_traveled
            current_leg_height = get_height_for_step(distance_traveled, total_distance, leg_lift_height)
            for new_leg_pos, start_leg_pos in zip(new_position.get_legs_as_list(forward_legs), start_position.get_legs_as_list(forward_legs)):
                new_leg_pos.z = start_leg_pos.z + current_leg_height
            self.__last_written_position = new_position
            self.ik_driver.move_legs_synced(self.__last_written_position)
            # print RELAXED_POSITION - self.__last_written_position
            sleep(self.__update_delay * 0.001)
        print "Finished step"

    def __get_next_leg_combo(self):
        self.__last_used_forward_legs = LegFlags.RF_LR_CROSS if self.__last_used_forward_legs == LegFlags.LF_RR_CROSS else LegFlags.LF_RR_CROSS
        return self.__last_used_forward_legs