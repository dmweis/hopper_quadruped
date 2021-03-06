from __future__ import division
from __future__ import absolute_import
from Queue import Queue
import traceback
import rospy

from threading import Event
from .hexapod_ik_driver import LegPositions, Vector3, Vector2, LegFlags
from .hexapod_choreographer import Choreographer
from hopper_controller.msg import FoldCommand
from hopper_feet_sensors.msg import FeetSensorData
import math

INTERPOLATION_FREQUENCY = 60

LEG_HEIGHT = -9
LEG_DISTANCE_LONGITUDAL = 15
MIDDLE_LEG_LONGITUDAL_OFFSET = 7
LEG_DISTANCE_LATERAL = 18

RELAXED_POSITION = LegPositions(
    Vector3(LEG_DISTANCE_LATERAL, LEG_DISTANCE_LONGITUDAL, LEG_HEIGHT),
    Vector3(LEG_DISTANCE_LATERAL, -LEG_DISTANCE_LONGITUDAL, LEG_HEIGHT),
    Vector3(0, LEG_DISTANCE_LONGITUDAL + MIDDLE_LEG_LONGITUDAL_OFFSET, LEG_HEIGHT),
    Vector3(0, -LEG_DISTANCE_LONGITUDAL - MIDDLE_LEG_LONGITUDAL_OFFSET, LEG_HEIGHT),
    Vector3(-LEG_DISTANCE_LATERAL, LEG_DISTANCE_LONGITUDAL, LEG_HEIGHT),
    Vector3(-LEG_DISTANCE_LATERAL, -LEG_DISTANCE_LONGITUDAL, LEG_HEIGHT),
)

OFFSET_DISTANCE = 1.5
GROUND_LEG_HEIGHT = -3

GROUND_LEVEL_RELAXED_POSITION = LegPositions(
    Vector3(LEG_DISTANCE_LATERAL + OFFSET_DISTANCE, LEG_DISTANCE_LONGITUDAL + OFFSET_DISTANCE, GROUND_LEG_HEIGHT),
    Vector3(LEG_DISTANCE_LATERAL + OFFSET_DISTANCE, -LEG_DISTANCE_LONGITUDAL - OFFSET_DISTANCE, GROUND_LEG_HEIGHT),
    Vector3(0, LEG_DISTANCE_LONGITUDAL + MIDDLE_LEG_LONGITUDAL_OFFSET + OFFSET_DISTANCE, GROUND_LEG_HEIGHT),
    Vector3(0, -LEG_DISTANCE_LONGITUDAL - MIDDLE_LEG_LONGITUDAL_OFFSET - OFFSET_DISTANCE, GROUND_LEG_HEIGHT),
    Vector3(-LEG_DISTANCE_LATERAL - OFFSET_DISTANCE, LEG_DISTANCE_LONGITUDAL + OFFSET_DISTANCE, GROUND_LEG_HEIGHT),
    Vector3(-LEG_DISTANCE_LATERAL - OFFSET_DISTANCE, -LEG_DISTANCE_LONGITUDAL - OFFSET_DISTANCE, GROUND_LEG_HEIGHT),
)

WIDER_RELAXED_POSITION = LegPositions(
    Vector3(LEG_DISTANCE_LATERAL + OFFSET_DISTANCE, LEG_DISTANCE_LONGITUDAL + OFFSET_DISTANCE, LEG_HEIGHT),
    Vector3(LEG_DISTANCE_LATERAL + OFFSET_DISTANCE, -LEG_DISTANCE_LONGITUDAL - OFFSET_DISTANCE, LEG_HEIGHT),
    Vector3(0, LEG_DISTANCE_LONGITUDAL + MIDDLE_LEG_LONGITUDAL_OFFSET + OFFSET_DISTANCE, LEG_HEIGHT),
    Vector3(0, -LEG_DISTANCE_LONGITUDAL - MIDDLE_LEG_LONGITUDAL_OFFSET - OFFSET_DISTANCE, LEG_HEIGHT),
    Vector3(-LEG_DISTANCE_LATERAL - OFFSET_DISTANCE, LEG_DISTANCE_LONGITUDAL + OFFSET_DISTANCE, LEG_HEIGHT),
    Vector3(-LEG_DISTANCE_LATERAL - OFFSET_DISTANCE, -LEG_DISTANCE_LONGITUDAL - OFFSET_DISTANCE, LEG_HEIGHT),
)


def get_height_for_step(distance, full_step_length, height):
    """
    :param distance: distance along the step
    :type distance: float
    :param full_step_length: full length of the step
    :type full_step_length: float
    :param height: max height that the leg should lift to
    :type height: float
    :return:
    """
    if full_step_length == 0:
        return 0
    distance = distance / full_step_length
    return max(math.sin(distance * math.pi) * height, 0)


class MovementController(object):
    def __init__(self, gait_engine, speech_service, folding_manager, state_telemetry_publisher, leg_controller, lidar_controller):
        """
        :type gait_engine: GaitEngine
        """
        super(MovementController, self).__init__()
        self.keep_running = True
        self._gait_engine = gait_engine
        self.folding_manager = folding_manager
        self.state_telemetry_publisher = state_telemetry_publisher
        self.choreographer = Choreographer(self._gait_engine)
        self._speech_service = speech_service
        self.leg_controller = leg_controller
        self.lidar_controller = lidar_controller

        self._relaxed = True
        self.relaxed_finished_event = Event()
        self._velocity = Vector2()
        self._theta = 0
        self._cycle_time = 1.0
        self._relaxed_transformation = Vector3()
        self._relaxed_rotation = Vector3()
        self._lift_height = 2
        self._pose_update_ready = False
        # single leg mode
        self.single_leg_mode_on = False
        self.selected_leg_switched = False
        self.selected_single_leg = int(LegFlags.LEFT_FRONT)
        self.single_leg_position = Vector3()
        self.single_leg_fast_mode = False
        self._command_queue = Queue(maxsize=1)
        self._ros_timer = rospy.Rate(INTERPOLATION_FREQUENCY)
        # sitting mode
        self.stand_mode = bool(rospy.get_param("stand_up_at_startup", False))
        self.currently_standing = False
        self.folding_operation = None

    def spin(self):
        self.state_telemetry_publisher.ready()
        try:
            self._main_controller_loop()
        except Exception as e:
            self._log_current_state()
            self._speech_service.say("ik_failure")
            rospy.logfatal("Gait engine loop failed " + str(e) + "\n" + traceback.format_exc())
            rospy.signal_shutdown("Gait engine loop failed " + str(e))

    def _main_controller_loop(self):
        rospy.loginfo("Hexapod gait engine started")
        self._speech_service.say("initialized_successfully")
        while not rospy.is_shutdown() and self.keep_running:
            if self.stand_mode and not self.currently_standing:
                if self.folding_manager.check_if_folded():
                    self.folding_manager.unfold()
                self._gait_engine.stand_up()
                self.currently_standing = True
            elif not self.stand_mode and self.currently_standing:
                self._gait_engine.sit_down()
                self.currently_standing = False
            if self.currently_standing:
                self.moving_mode_loop_tick()
            elif self.folding_operation:
                if self.folding_operation == FoldCommand.FOLD and not self.folding_manager.check_if_folded():
                    self.lidar_controller.stop()
                    self.folding_manager.fold()
                elif self.folding_operation == FoldCommand.UNFOLD and self.folding_manager.check_if_folded():
                    self.lidar_controller.start()
                    self.folding_manager.unfold()
                elif self.folding_operation == FoldCommand.FOLD_GROUNDED and not self.folding_manager.check_if_folded():
                    self.lidar_controller.stop()
                    self.folding_manager.fold_on_ground()
                elif self.folding_operation == FoldCommand.UNFOLD_GROUNDED and self.folding_manager.check_if_folded():
                    self.lidar_controller.start()
                    self.folding_manager.unfold_on_ground()
                self.folding_operation = None
        if self.currently_standing:
            self._gait_engine.sit_down()
        self._gait_engine.stop()

    def moving_mode_loop_tick(self):
        if self.single_leg_mode_on:
            self.handle_single_leg_mode()
        elif self.leg_controller.is_motion_queued():
            self.leg_controller.execute_motion()
        elif self.selected_leg_switched:
            self.selected_leg_switched = False
            self._gait_engine.move_to_new_pose(self._gait_engine.get_relaxed_pose(), 15)
        elif self._should_move():
            # execute move
            self._gait_engine.step(self._velocity, self._theta, self._cycle_time, self._lift_height)
            self._relaxed = False
        elif not self._relaxed:
            # go to relaxed
            self._gait_engine.relax_next_leg(self._cycle_time, self._lift_height)
            if not self._should_move():
                self._gait_engine.relax_next_leg(self._cycle_time, self._lift_height)
                self._relaxed = True
                self.relaxed_finished_event.set()
        elif self._pose_update_ready:
            # update pose
            self._pose_update_ready = False
            self._gait_engine.update_relaxed_body_pose(self._relaxed_transformation, self._relaxed_rotation)
        else:
            # execute any scheduled moves
            self._check_and_execute_scheduled_move()
            # this will sleep enough to maintain correct frequency
            self._ros_timer.sleep()

    def set_relaxed_pose(self, transform, rotation):
        """
        :type transform: Vector3
        :type rotation: Vector3
        """
        self._relaxed_transformation = transform
        self._relaxed_rotation = rotation
        self._pose_update_ready = True

    def execute_step_to_relaxed(self):
        self.relaxed_finished_event.clear()
        self._relaxed = False
        self.relaxed_finished_event.wait()

    def set_move_command(self, direction, rotation, cycle_time, lift_height):
        """
        :type direction: Vector2
        :type rotation: float
        :type cycle_time: float
        :type lift_height: float

        """
        self._velocity = direction
        self._theta = rotation
        self._lift_height = lift_height
        self._cycle_time = cycle_time

    def set_direction(self, direction, rotation):
        """

        :type direction: Vector2
        :type rotation: float
        """
        self._velocity = direction
        self._theta = rotation

    def stop_moving(self):
        self._velocity = Vector2()
        self._theta = 0

    def schedule_move(self, move_name):
        if move_name.lower() == "cancel":
            self.cancel_dance_moves()
            return
        if not self._command_queue.full():
            self._command_queue.put_nowait(move_name)

    def cancel_dance_moves(self):
        while not self._command_queue.empty():
            self._command_queue.get()
        self.choreographer.cancel_move()

    def update_single_leg_command(self, selected_leg, position, single_leg_mode_on, fast_mode):
        new_selected_leg = LegFlags(selected_leg)
        if new_selected_leg != self.selected_single_leg:
            self.selected_leg_switched = True
        self.selected_single_leg = new_selected_leg
        self.single_leg_position = position
        if self.single_leg_mode_on and not single_leg_mode_on:
            self.selected_leg_switched = True
        self.single_leg_fast_mode = fast_mode
        self.single_leg_mode_on = single_leg_mode_on

    def handle_single_leg_mode(self):
        leg_dict = {
            LegFlags.LEFT_FRONT: "left_front",
            LegFlags.LEFT_MIDDLE: "left_middle",
            LegFlags.LEFT_REAR: "left_rear",
            LegFlags.RIGHT_FRONT: "right_front",
            LegFlags.RIGHT_MIDDLE: "right_middle",
            LegFlags.RIGHT_REAR: "right_rear"
        }
        base_vector_dict = rospy.get_param("legs")[leg_dict[self.selected_single_leg]]["position"]
        lifted_vector = Vector3(base_vector_dict["x"] * 0.5, base_vector_dict["y"] * 0.5, base_vector_dict["z"] + 5)
        translation_vector = self.single_leg_position.clone()
        new_lifted_leg_pos = self._gait_engine.get_relaxed_pose()\
            .transform(lifted_vector, self.selected_single_leg)\
            .transform(translation_vector, self.selected_single_leg)
        if self.selected_leg_switched:
            self.selected_leg_switched = False
            self._gait_engine.move_to_new_pose(self._gait_engine.get_relaxed_pose(), 15)
        if self.single_leg_mode_on:
            if self.single_leg_fast_mode:
                self._gait_engine.move_to_new_pose(new_lifted_leg_pos, 100)
            else:
                self._gait_engine.move_to_new_pose(new_lifted_leg_pos, 15)

    def _should_move(self):
        return not self._velocity.is_zero() or self._theta != 0

    def _check_and_execute_scheduled_move(self):
        while not self._command_queue.empty():
            command = self._command_queue.get()
            self.choreographer.execute_choreography(command)

    def _log_current_state(self):
        data = {'direction': self._velocity,
                'rotation': self._theta,
                'relaxed_transformation': self._relaxed_transformation,
                'relaxed_rotation': self._relaxed_rotation}
        rospy.loginfo("Current hexapod state: %s", str(data))


class GaitEngine(object):
    def __init__(self, gait_sequencer_list):
        """
        :type gait_sequencer: TripodGait
        """
        super(GaitEngine, self).__init__()
        self.gait_sequencer_list = gait_sequencer_list
        self.gait_sequencer_index = 0
        self.gait_sequencer = self.gait_sequencer_list[self.gait_sequencer_index]
        self._last_used_lifted_legs = LegFlags.LEFT_TRIPOD
        self.stand_up_leg_order = [LegFlags.MIDDLE, LegFlags.FRONT | LegFlags.REAR]
        # time each step takes in seconds
        self._default_cycle_time = 1.0

    def switch_gait_sequencer(self):
        self.gait_sequencer_index+=1
        if self.gait_sequencer_index >= len(self.gait_sequencer_list):
            self.gait_sequencer_index = 0
        self.gait_sequencer = self.gait_sequencer_list[self.gait_sequencer_index]

    def stand_up(self, speed=11):
        rospy.loginfo("Hexapod gait engine started")
        self.gait_sequencer.read_current_position()
        for leg in self.stand_up_leg_order:
            new_position = self.gait_sequencer.last_written_position.clone().update_from_other(GROUND_LEVEL_RELAXED_POSITION, leg)
            self.gait_sequencer.execute_move(new_position, 15)
        self.gait_sequencer.execute_move(WIDER_RELAXED_POSITION.clone(), speed)
        self.gait_sequencer.go_to_relaxed(self._get_next_leg_combo(), self.gait_sequencer.current_relaxed_position, 0.5)
        self.gait_sequencer.go_to_relaxed(self._get_next_leg_combo(), self.gait_sequencer.current_relaxed_position, 0.5)
        rospy.loginfo("Hexapod ready")

    def step(self, direction, rotation, cycle_time, lift_height=2.0):
        """
        :type direction: Vector2
        :type rotation: float
        :type cycle_time: float
        :type lift_height: float
        """
        self.gait_sequencer.execute_step(direction, rotation, self._get_next_leg_combo(),
                                         cycle_time,
                                         leg_lift_height=lift_height)

    def relax_next_leg(self, cycle_time, leg_lift_height):
        self.gait_sequencer.go_to_relaxed(self._get_next_leg_combo(), self.gait_sequencer.current_relaxed_position, cycle_time, leg_lift_height=leg_lift_height)

    def move_to_new_pose(self, pose, speed_override):
        """
        :type pose: LegPositions
        :type speed_override: float
        """
        self.gait_sequencer.execute_move(pose, speed_override)

    def update_relaxed_body_pose(self, transform, rotation, legs=LegFlags.ALL, speed_override=None):
        speed = 15
        if speed_override is not None:
            speed = speed_override
        self.gait_sequencer.update_relaxed_body_pose(transform, rotation, speed, legs)

    def sit_down(self, speed=11):
        self.gait_sequencer.reset_relaxed_body_pose(speed_override=speed)
        self.gait_sequencer.go_to_relaxed(self._get_next_leg_combo(), WIDER_RELAXED_POSITION, 0.5)
        self.gait_sequencer.go_to_relaxed(self._get_next_leg_combo(), WIDER_RELAXED_POSITION, 0.5)
        self.gait_sequencer.execute_move(GROUND_LEVEL_RELAXED_POSITION.clone(), 6)
        self.gait_sequencer.stop()

    def reset_relaxed_body_pose(self, speed_override=9):
        self.gait_sequencer.reset_relaxed_body_pose(speed_override=speed_override)

    def get_relaxed_pose(self):
        return self.gait_sequencer.current_relaxed_position.clone()

    def get_current_leg_positions(self):
        return self.gait_sequencer.last_written_position.clone()

    def _get_next_leg_combo(self):
        self._last_used_lifted_legs = LegFlags.RIGHT_TRIPOD if self._last_used_lifted_legs == LegFlags.LEFT_TRIPOD else LegFlags.LEFT_TRIPOD
        return self._last_used_lifted_legs

    def stop(self):
        self.gait_sequencer.stop()


class TripodGait(object):
    def __init__(self, ik_driver, height_publisher, velocity_publisher):
        super(TripodGait, self).__init__()
        self._ik_driver = ik_driver
        self._height_publisher = height_publisher
        self._velocity_publisher = velocity_publisher
        self._ros_rate = rospy.Rate(INTERPOLATION_FREQUENCY)
        self._update_delay = 1000 / INTERPOLATION_FREQUENCY
        self.current_relaxed_position = RELAXED_POSITION.clone()
        self.last_written_position = self._ik_driver.read_current_leg_positions()

    def stop(self, disable_motors=True):
        if disable_motors:
            self._ik_driver.disable_motors()

    def update_relaxed_body_pose(self, transform, rotation, speed, legs=LegFlags.ALL):
        """
        :type transform: Vector3
        :type rotation: Vector3
        :type speed: float
        :type legs: LegFlags
        """
        self.current_relaxed_position = RELAXED_POSITION.clone() \
            .transform(transform * -1, legs) \
            .rotate(rotation, legs)
        self.execute_move(self.current_relaxed_position, speed)

    def reset_relaxed_body_pose(self, speed_override=None):
        self.current_relaxed_position = RELAXED_POSITION.clone()
        self.execute_move(self.current_relaxed_position, speed_override)

    def read_current_position(self):
        self.last_written_position = self._ik_driver.read_current_leg_positions()

    def execute_step(self, velocity, theta, lifted_legs, cycle_length, leg_lift_height=2.0):
        """
        :param velocity: Velocities in X and Y in cm/s
        :param theta: rotation speed in degrees per second
        :param lifted_legs: combination of legs to lift
        :param cycle_length: length of this step cycle in seconds
        :param leg_lift_height: height to which legs should be lifted from ground
        """
        # calculate traveled distance based on speed and velocity
        distance = Vector2()
        distance.x = velocity.x * cycle_length
        distance.y = velocity.y * cycle_length
        angle = theta * cycle_length
        self._velocity_publisher.update_velocity(velocity, theta)
        grounded_legs = LegFlags.RIGHT_TRIPOD if lifted_legs == LegFlags.LEFT_TRIPOD else LegFlags.LEFT_TRIPOD
        start_position = self.last_written_position.clone()
        # calculate target position
        grounded_legs_vector_to_relaxed = self.current_relaxed_position.get_center_point(grounded_legs)\
                                              - start_position.get_center_point(grounded_legs)
        target_position = self.current_relaxed_position.clone() \
            .transform(Vector3(distance.x / 2, distance.y / 2, 0), lifted_legs) \
            .turn(angle / 2, lifted_legs) \
            .update_from_other(start_position, grounded_legs) \
            .transform(grounded_legs_vector_to_relaxed, grounded_legs) \
            .transform(Vector3(-distance.x / 2, -distance.y / 2, 0), grounded_legs) \
            .turn(-angle / 2, grounded_legs)
        self._step_to_position(lifted_legs, target_position, cycle_length, leg_lift_height, velocity)
        self._velocity_publisher.update_move(velocity * cycle_length)
        self._velocity_publisher.update_velocity(Vector2(), 0)

    def go_to_relaxed(self, lifted_legs, target_stance, cycle_length, leg_lift_height=2):
        start_position = self.last_written_position.clone()
        target_position = start_position.update_from_other(target_stance, lifted_legs)
        self._step_to_position(lifted_legs, target_position, cycle_length, leg_lift_height)

    def _step_to_position(self, lifted_legs, target_position, cycle_length, leg_lift_height, velocity=Vector2()):
        start_position = self.last_written_position.clone()
        start_time = rospy.get_time()
        step_time = 0.0
        while step_time <= cycle_length:
            step_time = rospy.get_time() - start_time
            step_portion = step_time / cycle_length
            current_position_on_ground = start_position.get_moved_towards_by_portion(target_position, step_portion)
            new_position = current_position_on_ground.clone()
            for new_leg_pos, start_leg_pos, target_leg_pos in zip(new_position.get_legs_as_list(lifted_legs),
                                                                  start_position.get_legs_as_list(lifted_legs),
                                                                  target_position.get_legs_as_list(lifted_legs)):
                new_leg_pos.z = start_leg_pos.z + get_height_for_step((new_leg_pos - start_leg_pos).length(),
                                                                      (target_leg_pos - start_leg_pos).length(),
                                                                      leg_lift_height)
            self.last_written_position = new_position
            self._ik_driver.move_legs_synced(self.last_written_position)
            self._velocity_publisher.temporary_update_move(velocity * cycle_length * step_portion)
            self.publish_height()
            self._ros_rate.sleep()

    def execute_move(self, target_position, speed):
        while self.last_written_position.move_towards_at_speed(target_position, speed * 0.001 * self._update_delay):
            self._ik_driver.move_legs_synced(self.last_written_position)
            self.publish_height()
            self._ros_rate.sleep()
        self._ik_driver.move_legs_synced(self.last_written_position)
        self.publish_height()
        self._ros_rate.sleep()

    def publish_height(self):
        heights = [-x.z for x in self.last_written_position.get_legs_as_list(LegFlags.ALL)]
        tallest = sorted(heights, reverse=True)[:3]
        average_height = sum(tallest) / len(tallest)
        self._height_publisher.update_height(average_height)


def move_number_towards(number, target, max_delta):
    distance = abs(target - number)
    # if no distance to travel just finish
    if distance == 0:
        return False, target
    if max_delta >= distance:
        return True, target
    return True, number + math.copysign(max_delta, target)


class HeightAdjustTripodGait(object):
    def __init__(self, ik_driver, height_publisher, velocity_publisher):
        super(HeightAdjustTripodGait, self).__init__()
        self._ik_driver = ik_driver
        self._height_publisher = height_publisher
        self._velocity_publisher = velocity_publisher
        self._ros_rate = rospy.Rate(INTERPOLATION_FREQUENCY)
        self._update_delay = 1000 / INTERPOLATION_FREQUENCY
        self.current_relaxed_position = RELAXED_POSITION.clone()
        self.last_written_position = self._ik_driver.read_current_leg_positions()
        # leg sensor subscriber
        self.last_feet_sensors = FeetSensorData()
        rospy.Subscriber("hopper/feet", FeetSensorData, self.on_feet_data, queue_size=10)

    def stop(self, disable_motors=True):
        if disable_motors:
            self._ik_driver.disable_motors()

    def on_feet_data(self, msg):
        self.last_feet_sensors = msg

    def is_touching_ground(self, leg_flag):
        mapping = {
            LegFlags.LEFT_FRONT: self.last_feet_sensors.left_front,
            LegFlags.LEFT_MIDDLE: self.last_feet_sensors.left_middle,
            LegFlags.LEFT_REAR: self.last_feet_sensors.left_rear,
            LegFlags.RIGHT_FRONT: self.last_feet_sensors.right_front,
            LegFlags.RIGHT_MIDDLE: self.last_feet_sensors.right_middle,
            LegFlags.RIGHT_REAR: self.last_feet_sensors.right_rear
        }
        return mapping[leg_flag]

    def update_relaxed_body_pose(self, transform, rotation, speed, legs=LegFlags.ALL):
        """
        :type transform: Vector3
        :type rotation: Vector3
        :type speed: float
        :type legs: LegFlags
        """
        self.current_relaxed_position = RELAXED_POSITION.clone() \
            .transform(transform * -1, legs) \
            .rotate(rotation, legs)
        self.execute_move(self.current_relaxed_position, speed)

    def reset_relaxed_body_pose(self, speed_override=None):
        self.current_relaxed_position = RELAXED_POSITION.clone()
        self.execute_move(self.current_relaxed_position, speed_override)

    def read_current_position(self):
        self.last_written_position = self._ik_driver.read_current_leg_positions()

    def execute_step(self, velocity, theta, lifted_legs, cycle_length, leg_lift_height=2.0):
        """
        :param velocity: Velocities in X and Y in cm/s
        :param theta: rotation speed in degrees per second
        :param lifted_legs: combination of legs to lift
        :param cycle_length: length of this step cycle in seconds
        :param leg_lift_height: height to which legs should be lifted from ground
        """
        # calculate traveled distance based on speed and velocity
        distance = Vector2()
        distance.x = velocity.x * cycle_length
        distance.y = velocity.y * cycle_length
        angle = theta * cycle_length
        self._velocity_publisher.update_velocity(velocity, theta)
        grounded_legs = LegFlags.RIGHT_TRIPOD if lifted_legs == LegFlags.LEFT_TRIPOD else LegFlags.LEFT_TRIPOD
        start_position = self.last_written_position.clone()
        # calculate target position
        grounded_legs_vector_to_relaxed = self.current_relaxed_position.get_center_point(grounded_legs)\
                                              - start_position.get_center_point(grounded_legs)
        target_position = self.current_relaxed_position.clone() \
            .transform(Vector3(distance.x / 2, distance.y / 2, 0), lifted_legs) \
            .turn(angle / 2, lifted_legs) \
            .update_from_other(start_position, grounded_legs) \
            .transform(grounded_legs_vector_to_relaxed, grounded_legs) \
            .transform(Vector3(-distance.x / 2, -distance.y / 2, 0), grounded_legs) \
            .turn(-angle / 2, grounded_legs)
        self._step_to_position(lifted_legs, target_position, cycle_length, leg_lift_height, velocity)
        self._velocity_publisher.update_move(velocity * cycle_length)
        self._velocity_publisher.update_velocity(Vector2(), 0)

    def go_to_relaxed(self, lifted_legs, target_stance, cycle_length, leg_lift_height=2):
        start_position = self.last_written_position.clone()
        target_position = start_position.update_from_other(target_stance, lifted_legs)
        self._step_to_position(lifted_legs, target_position, cycle_length, leg_lift_height)

    def _step_to_position(self, lifted_legs, target_position, cycle_length, leg_lift_height, velocity=Vector2()):
        start_position = self.last_written_position.clone()
        start_time = rospy.get_time()
        step_time = 0.0

        lowest_foot_height = min([pos.z for pos in self.last_written_position.get_legs_as_list()])
        max_foot_lift = lowest_foot_height + leg_lift_height

        all_feet_lowered = False
        while step_time <= cycle_length or not all_feet_lowered:
            step_time = rospy.get_time() - start_time
            step_portion = step_time / cycle_length
            step_portion = min(step_portion, 1.0)
            current_position_on_ground = start_position.get_moved_towards_by_portion(target_position, step_portion)
            new_position = current_position_on_ground.clone()
            all_feet_lowered = True
            for new_leg_pos, start_leg_pos, target_leg_pos, leg_flag in zip(new_position.get_legs_as_list(lifted_legs),
                                                                  start_position.get_legs_as_list(lifted_legs),
                                                                  target_position.get_legs_as_list(lifted_legs),
                                                                  LegFlags.get_legs_as_list(lifted_legs)):
                foot_lowered = False
                if step_portion < 0.9:
                    new_leg_pos.z = start_leg_pos.z + leg_lift_height
                elif self.is_touching_ground(leg_flag):
                    new_leg_pos.z = self.last_written_position.get_legs_as_list(leg_flag)[0].z
                    foot_lowered = True
                else:
                    # slowly lower leg at the end of motion
                    
                    moved, new_leg_pos.z = move_number_towards(self.last_written_position.get_legs_as_list(leg_flag)[0].z, start_leg_pos.z, 12 / INTERPOLATION_FREQUENCY)
                    foot_lowered = not moved
                new_leg_pos.z = min(new_leg_pos.z, max_foot_lift)
                all_feet_lowered = all_feet_lowered and foot_lowered
            self.last_written_position = new_position
            self._ik_driver.move_legs_synced(self.last_written_position)
            self._velocity_publisher.temporary_update_move(velocity * cycle_length * step_portion)
            self.publish_height()
            self._ros_rate.sleep()

    def execute_move(self, target_position, speed):
        while self.last_written_position.move_towards_at_speed(target_position, speed * 0.001 * self._update_delay):
            self._ik_driver.move_legs_synced(self.last_written_position)
            self.publish_height()
            self._ros_rate.sleep()
        self._ik_driver.move_legs_synced(self.last_written_position)
        self.publish_height()
        self._ros_rate.sleep()

    def publish_height(self):
        heights = [-x.z for x in self.last_written_position.get_legs_as_list(LegFlags.ALL)]
        tallest = sorted(heights, reverse=True)[:3]
        average_height = sum(tallest) / len(tallest)
        self._height_publisher.update_height(average_height)

