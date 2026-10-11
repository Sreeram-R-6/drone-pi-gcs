#!/usr/bin/env python3

import math
import socket
import time
import threading

import cv2
import numpy as np
import quirc

import rclpy
from rclpy.node import Node

from rclpy.qos import (
    QoSProfile,
    ReliabilityPolicy,
    HistoryPolicy
)

from geometry_msgs.msg import PoseStamped
from sensor_msgs.msg import Image
from std_msgs.msg import String

from mavros_msgs.msg import State, PositionTarget
from mavros_msgs.srv import (
    SetMode,
    CommandBool,
    CommandLong,
    ParamSet
)

from cv_bridge import CvBridge

from PIL import Image as PILImage
from PIL import ImageOps


# ==========================================================
# FLIGHT
# ==========================================================

TAKEOFF_ALTITUDE = 10.0
TAKEOFF_TOLERANCE = 0.4

QR_SEARCH_ALTITUDE = 1.2
QR_SEARCH_TOLERANCE = 0.20


# ==========================================================
# SEARCH MOTION
# ==========================================================

SEARCH_SPEED_CM_S = 10.0
SEARCH_ACCEL_CM_S2 = 5.0
SEARCH_JERK = 0.5


# ==========================================================
# SEARCH PATTERN
# ==========================================================

SEARCH_LENGTH = 15.0

SEARCH_Y_MIN = -5.0
SEARCH_Y_MAX = 5.0

SEARCH_TRACK_SPACING = 2.0

SEARCH_TOLERANCE = 0.30

SEARCH_WAYPOINT_HOLD = 2.5


# ==========================================================
# CAMERA
# ==========================================================

IMAGE_WIDTH = 640.0
IMAGE_HEIGHT = 360.0

HORIZONTAL_FOV = math.radians(90.0)

VERTICAL_FOV = (
    2.0 *
    math.atan(
        math.tan(HORIZONTAL_FOV / 2.0) *
        IMAGE_HEIGHT /
        IMAGE_WIDTH
    )
)


# ==========================================================
# WHITE TARGET DETECTION
# ==========================================================

MIN_WHITE_AREA = 2500.0
MAX_WHITE_AREA = 180000.0

MIN_ASPECT_RATIO = 0.55
MAX_ASPECT_RATIO = 1.8

WHITE_CONFIRMATIONS_REQUIRED = 6


# ==========================================================
# GEOTAG
# ==========================================================

QR_GEOTAG_SCAN_TIME = 5.0

GEOTAG_GAIN = 1.0

GEOTAG_TOLERANCE = 0.40


# ==========================================================
# CENTERING
# ==========================================================

CENTER_PIXEL_TOLERANCE = 22.0

CENTER_CONFIRMATIONS_REQUIRED = 12

MAX_CENTER_SPEED = 0.15

MAX_CENTER_ACCEL = 0.08

CENTER_CONTROL_RATE = 20.0

CENTER_FILTER_ALPHA = 0.25

CENTER_GAIN = 0.45


# ==========================================================
# DESCENT
# ==========================================================

DESCENT_STEP = 0.30

DESCENT_COMMAND_INTERVAL = 0.50


# ==========================================================
# QR
# ==========================================================

QR_CONFIRMATIONS_REQUIRED = 5

# Maximum time to search for the QR after reaching
# the final low-altitude search height.
QR_WAIT_TIMEOUT = 30.0


# ==========================================================
# GCS UDP
# ==========================================================

GCS_IP = "127.0.0.1"
GCS_PORT = 5005

UDP_SEND_COUNT = 3
UDP_SEND_INTERVAL = 0.20


class GeoTagMission(Node):

    def __init__(self):

        super().__init__('geotag_mission')

        # ==================================================
        # STATE
        # ==================================================

        self.state = State()

        self.pose = PoseStamped()

        self.pose_received = False


        # ==================================================
        # CAMERA
        # ==================================================

        self.bridge = CvBridge()

        camera_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
            reliability=ReliabilityPolicy.BEST_EFFORT
        )

        self.image_sub = self.create_subscription(
            Image,
            '/camera/down/image',
            self.image_callback,
            camera_qos
        )


        # ==================================================
        # MAVROS
        # ==================================================

        self.state_sub = self.create_subscription(
            State,
            '/mavros/state',
            self.state_callback,
            10
        )

        self.pose_sub = self.create_subscription(
            PoseStamped,
            '/mavros/local_position/pose',
            self.pose_callback,
            camera_qos
        )

        self.setpoint_pub = self.create_publisher(
            PoseStamped,
            '/mavros/setpoint_position/local',
            10
        )

        self.velocity_pub = self.create_publisher(
            PositionTarget,
            '/mavros/setpoint_raw/local',
            10
        )

        self.qr_pub = self.create_publisher(
            String,
            '/rescue/qr_detected',
            10
        )


        # ==================================================
        # SERVICES
        # ==================================================

        self.mode_client = self.create_client(
            SetMode,
            '/mavros/set_mode'
        )

        self.arm_client = self.create_client(
            CommandBool,
            '/mavros/cmd/arming'
        )

        self.command_client = self.create_client(
            CommandLong,
            '/mavros/cmd/command'
        )

        self.param_client = self.create_client(
            ParamSet,
            '/mavros/param/set'
        )


        # ==================================================
        # UDP
        # ==================================================

        self.udp_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM
        )

        self.udp_sent = False


        # ==================================================
        # TIMER
        # ==================================================

        self.timer = self.create_timer(
            0.05,
            self.mission_loop
        )


        # ==================================================
        # START CONFIRMATION
        # ==================================================

        self.start_event = threading.Event()

        self.start_prompted = False


        # ==================================================
        # PHASE
        # ==================================================

        self.phase = 'WAIT_FCU'

        self.phase_start = (
            self.get_clock().now()
        )


        # ==================================================
        # TAKEOFF
        # ==================================================

        self.takeoff_sent = False


        # ==================================================
        # SEARCH
        # ==================================================

        self.search_origin_x = 0.0
        self.search_origin_y = 0.0

        self.search_waypoints = []

        self.search_index = 0

        self.search_reached_time = None


        # ==================================================
        # WHITE TARGET
        # ==================================================

        self.white_cx = 0.0
        self.white_cy = 0.0

        self.white_width = 0.0
        self.white_height = 0.0

        self.white_confirmations = 0


        # ==================================================
        # GEOTAG
        # ==================================================

        self.qr_geotag_x = 0.0
        self.qr_geotag_y = 0.0

        self.geotag_valid = False


        # ==================================================
        # CENTERING
        # ==================================================

        self.filtered_cx = None
        self.filtered_cy = None

        self.center_confirmations = 0

        self.center_velocity_x = 0.0
        self.center_velocity_y = 0.0

        self.last_center_time = 0.0

        self.center_target_x = 0.0
        self.center_target_y = 0.0


        # ==================================================
        # DESCENT
        # ==================================================

        self.descent_target_z = TAKEOFF_ALTITUDE

        self.descent_command_time = 0.0


        # ==================================================
        # QR
        # ==================================================

        self.last_qr = ""

        self.qr_confirmations = 0

        self.qr_confirmed = False

        self.qr_value = ""


        # ==================================================
        # RTL
        # ==================================================

        self.rtl_sent = False


        # ==================================================
        # PARAMETERS
        # ==================================================

        self.params_set = False


        self.get_logger().info(
            'GEOTAG AUTONOMOUS RESCUE MISSION STARTED'
        )


    # ======================================================
    # CALLBACKS
    # ======================================================

    def state_callback(self, msg):

        self.state = msg


    def pose_callback(self, msg):

        self.pose = msg
        self.pose_received = True


    # ======================================================
    # START CONFIRMATION
    # ======================================================

    def wait_for_start_confirmation(self):

        self.get_logger().info(
            '=========================================='
        )

        self.get_logger().info(
            'AUTONOMOUS MISSION READY'
        )

        self.get_logger().info(
            'Press ENTER to start the mission...'
        )

        try:
            input()
            self.start_event.set()

        except EOFError:
            self.get_logger().error(
                'START CONFIRMATION INPUT CLOSED'
            )


    # ======================================================
    # PHASE
    # ======================================================

    def set_phase(self, phase):

        if self.phase == phase:
            return

        self.phase = phase

        self.phase_start = (
            self.get_clock().now()
        )

        self.get_logger().info(
            f'PHASE -> {phase}'
        )


    def elapsed(self):

        return (
            self.get_clock().now()
            -
            self.phase_start
        ).nanoseconds / 1e9


    # ======================================================
    # QR RESET
    # ======================================================

    def reset_qr_tracking(self):

        self.last_qr = ""

        self.qr_confirmations = 0

        self.qr_confirmed = False

        self.qr_value = ""


    # ======================================================
    # RESUME SEARCH
    # ======================================================

    def resume_search(self):

        self.get_logger().warn(
            'QR NOT FOUND - RESUMING SEARCH'
        )

        self.reset_qr_tracking()

        self.white_confirmations = 0

        self.filtered_cx = None
        self.filtered_cy = None

        self.center_confirmations = 0

        self.center_velocity_x = 0.0
        self.center_velocity_y = 0.0

        self.last_center_time = 0.0

        self.search_reached_time = None

        # Continue with the next search waypoint so the
        # vehicle does not immediately repeat the same area.
        if self.search_index < len(
            self.search_waypoints
        ):

            self.search_index += 1

        self.set_phase('SEARCH')


    # ======================================================
    # PARAMETERS
    # ======================================================

    def set_param(self, name, value):

        if not self.param_client.service_is_ready():
            return

        req = ParamSet.Request()

        req.param_id = name

        req.value.real = float(value)

        future = (
            self.param_client.call_async(req)
        )

        future.add_done_callback(
            lambda f, n=name, v=value:
            self.param_response(f, n, v)
        )


    def param_response(
        self,
        future,
        name,
        value
    ):

        try:

            response = future.result()

            if response.success:

                self.get_logger().info(
                    f'PARAM {name} = {value}'
                )

            else:

                self.get_logger().error(
                    f'FAILED TO SET {name}'
                )

        except Exception as e:

            self.get_logger().error(
                f'Parameter error: {e}'
            )


    def configure_search_motion(self):

        if self.params_set:
            return

        if not self.param_client.service_is_ready():
            return

        self.get_logger().info(
            'Configuring search motion'
        )

        self.set_param(
            'WPNAV_SPEED',
            SEARCH_SPEED_CM_S
        )

        self.set_param(
            'WPNAV_ACCEL',
            SEARCH_ACCEL_CM_S2
        )

        self.set_param(
            'WPNAV_JERK',
            SEARCH_JERK
        )

        self.params_set = True


    # ======================================================
    # GUIDED
    # ======================================================

    def set_guided(self):

        if not self.mode_client.service_is_ready():
            return

        req = SetMode.Request()

        req.base_mode = 0
        req.custom_mode = 'GUIDED'

        self.mode_client.call_async(req)


    # ======================================================
    # ARM
    # ======================================================

    def arm(self):

        if not self.arm_client.service_is_ready():
            return

        req = CommandBool.Request()

        req.value = True

        self.arm_client.call_async(req)


    # ======================================================
    # TAKEOFF
    # ======================================================

    def send_takeoff(self):

        if not self.command_client.service_is_ready():
            return

        req = CommandLong.Request()

        req.broadcast = False
        req.command = 22
        req.confirmation = 0

        req.param1 = 0.0
        req.param2 = 0.0
        req.param3 = 0.0
        req.param4 = 0.0
        req.param5 = 0.0
        req.param6 = 0.0
        req.param7 = TAKEOFF_ALTITUDE

        future = (
            self.command_client.call_async(req)
        )

        future.add_done_callback(
            self.takeoff_response
        )


    def takeoff_response(self, future):

        try:

            response = future.result()

            self.get_logger().info(
                f'TAKEOFF ACK: '
                f'success={response.success} '
                f'result={response.result}'
            )

            if response.success:

                self.takeoff_sent = True

        except Exception as e:

            self.get_logger().error(
                f'Takeoff error: {e}'
            )


    # ======================================================
    # POSITION SETPOINT
    # ======================================================

    def publish_position_target(
        self,
        x,
        y,
        z
    ):

        msg = PoseStamped()

        msg.header.stamp = (
            self.get_clock().now().to_msg()
        )

        msg.pose.position.x = float(x)
        msg.pose.position.y = float(y)
        msg.pose.position.z = float(z)

        msg.pose.orientation.w = 1.0

        self.setpoint_pub.publish(msg)


    # ======================================================
    # VELOCITY SETPOINT
    # ======================================================

    def publish_center_velocity(
        self,
        vx,
        vy
    ):

        msg = PositionTarget()

        msg.header.stamp = (
            self.get_clock().now().to_msg()
        )

        msg.coordinate_frame = (
            PositionTarget.FRAME_BODY_NED
        )

        msg.type_mask = (
            PositionTarget.IGNORE_PX |
            PositionTarget.IGNORE_PY |
            PositionTarget.IGNORE_PZ |
            PositionTarget.IGNORE_AFX |
            PositionTarget.IGNORE_AFY |
            PositionTarget.IGNORE_AFZ |
            PositionTarget.IGNORE_YAW |
            PositionTarget.IGNORE_YAW_RATE
        )

        msg.velocity.x = float(vx)
        msg.velocity.y = float(vy)
        msg.velocity.z = 0.0

        self.velocity_pub.publish(msg)


    def stop_centering(self):

        self.center_velocity_x = 0.0
        self.center_velocity_y = 0.0

        for _ in range(3):

            self.publish_center_velocity(
                0.0,
                0.0
            )


    # ======================================================
    # SEARCH PATTERN
    # ======================================================

    def build_search_pattern(self):

        self.search_waypoints = []

        y = SEARCH_Y_MIN

        direction = 1

        while y <= SEARCH_Y_MAX + 0.01:

            if direction == 1:

                self.search_waypoints.append(
                    (
                        self.search_origin_x,
                        self.search_origin_y + y
                    )
                )

                self.search_waypoints.append(
                    (
                        self.search_origin_x +
                        SEARCH_LENGTH,
                        self.search_origin_y + y
                    )
                )

            else:

                self.search_waypoints.append(
                    (
                        self.search_origin_x +
                        SEARCH_LENGTH,
                        self.search_origin_y + y
                    )
                )

                self.search_waypoints.append(
                    (
                        self.search_origin_x,
                        self.search_origin_y + y
                    )
                )

            y += SEARCH_TRACK_SPACING

            direction *= -1


        self.get_logger().info(
            f'Search pattern: '
            f'{len(self.search_waypoints)} waypoints'
        )


    # ======================================================
    # SEARCH
    # ======================================================

    def execute_search(self):

        if self.search_index >= len(
            self.search_waypoints
        ):

            self.get_logger().error(
                'SEARCH COMPLETE - TARGET NOT FOUND'
            )

            self.set_phase('RTL')

            return


        target_x, target_y = (
            self.search_waypoints[
                self.search_index
            ]
        )


        self.publish_position_target(
            target_x,
            target_y,
            TAKEOFF_ALTITUDE
        )


        if not self.pose_received:
            return


        dx = (
            self.pose.pose.position.x -
            target_x
        )

        dy = (
            self.pose.pose.position.y -
            target_y
        )

        distance = math.sqrt(
            dx * dx +
            dy * dy
        )


        if distance <= SEARCH_TOLERANCE:

            if self.search_reached_time is None:

                self.search_reached_time = (
                    self.get_clock().now()
                )

                self.get_logger().info(
                    f'WAYPOINT '
                    f'{self.search_index + 1}/'
                    f'{len(self.search_waypoints)} '
                    f'reached - holding '
                    f'{SEARCH_WAYPOINT_HOLD:.1f}s'
                )

                return


            hold_time = (
                self.get_clock().now()
                -
                self.search_reached_time
            ).nanoseconds / 1e9


            if hold_time >= SEARCH_WAYPOINT_HOLD:

                self.search_index += 1

                self.search_reached_time = None

                self.get_logger().info(
                    'Continuing search'
                )

        else:

            self.search_reached_time = None


    # ======================================================
    # WHITE TARGET DETECTOR
    # ======================================================

    def detect_white_target(self, frame):

        hsv = cv2.cvtColor(
            frame,
            cv2.COLOR_BGR2HSV
        )

        mask = cv2.inRange(
            hsv,
            np.array([0, 0, 170]),
            np.array([180, 70, 255])
        )

        kernel = np.ones(
            (5, 5),
            np.uint8
        )

        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_OPEN,
            kernel
        )

        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            kernel
        )

        contours, _ = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )

        candidates = []


        for contour in contours:

            area = cv2.contourArea(
                contour
            )

            if area < MIN_WHITE_AREA:
                continue

            if area > MAX_WHITE_AREA:
                continue


            perimeter = cv2.arcLength(
                contour,
                True
            )

            if perimeter <= 0:
                continue


            approx = cv2.approxPolyDP(
                contour,
                0.04 * perimeter,
                True
            )

            if len(approx) < 4:
                continue


            x, y, w, h = (
                cv2.boundingRect(
                    contour
                )
            )

            if h <= 0:
                continue


            aspect = w / float(h)


            if (
                aspect < MIN_ASPECT_RATIO
                or
                aspect > MAX_ASPECT_RATIO
            ):
                continue


            hull = cv2.convexHull(
                contour
            )

            hull_area = cv2.contourArea(
                hull
            )

            if hull_area <= 0:
                continue


            solidity = (
                area /
                hull_area
            )


            if solidity < 0.80:
                continue


            candidates.append(
                (
                    area,
                    x,
                    y,
                    w,
                    h
                )
            )


        if not candidates:

            self.white_confirmations = 0

            return False


        candidates.sort(
            reverse=True
        )


        _, x, y, w, h = candidates[0]


        self.white_cx = (
            x +
            w / 2.0
        )

        self.white_cy = (
            y +
            h / 2.0
        )

        self.white_width = w
        self.white_height = h

        self.white_confirmations += 1


        return (
            self.white_confirmations >=
            WHITE_CONFIRMATIONS_REQUIRED
        )


    # ======================================================
    # QUATERNION -> YAW
    # ======================================================

    def get_yaw(self):

        q = self.pose.pose.orientation

        siny_cosp = (
            2.0 *
            (
                q.w * q.z +
                q.x * q.y
            )
        )

        cosy_cosp = (
            1.0 -
            2.0 *
            (
                q.y * q.y +
                q.z * q.z
            )
        )

        return math.atan2(
            siny_cosp,
            cosy_cosp
        )


    # ======================================================
    # CAMERA PIXEL -> GROUND OFFSET
    # ======================================================

    def image_to_ground_error(
        self,
        altitude
    ):

        error_px_x = (
            self.white_cx -
            IMAGE_WIDTH / 2.0
        )

        error_px_y = (
            self.white_cy -
            IMAGE_HEIGHT / 2.0
        )


        ground_width = (
            2.0 *
            altitude *
            math.tan(
                HORIZONTAL_FOV / 2.0
            )
        )


        ground_height = (
            2.0 *
            altitude *
            math.tan(
                VERTICAL_FOV / 2.0
            )
        )


        mx_pixel = (
            ground_width /
            IMAGE_WIDTH
        )

        my_pixel = (
            ground_height /
            IMAGE_HEIGHT
        )


        right = (
            -error_px_x *
            mx_pixel
        )


        forward = (
            -error_px_y *
            my_pixel
        )


        return forward, right


    # ======================================================
    # GEOTAG TARGET
    # ======================================================

    def calculate_geotag(self):

        if not self.pose_received:
            return False


        altitude = max(
            self.pose.pose.position.z,
            0.5
        )


        forward, right = (
            self.image_to_ground_error(
                altitude
            )
        )


        yaw = self.get_yaw()


        world_dx = (
            math.cos(yaw) * forward
            -
            math.sin(yaw) * right
        )

        world_dy = (
            math.sin(yaw) * forward
            +
            math.cos(yaw) * right
        )


        self.qr_geotag_x = (
            self.pose.pose.position.x
            +
            GEOTAG_GAIN *
            world_dx
        )

        self.qr_geotag_y = (
            self.pose.pose.position.y
            +
            GEOTAG_GAIN *
            world_dy
        )


        self.geotag_valid = True


        self.get_logger().info(
            '=========================================='
        )

        self.get_logger().info(
            'QR GEOTAG FOUND'
        )

        self.get_logger().info(
            f'Current drone position: '
            f'x={self.pose.pose.position.x:.3f} '
            f'y={self.pose.pose.position.y:.3f} '
            f'z={altitude:.3f}'
        )

        self.get_logger().info(
            f'Camera pixel: '
            f'({self.white_cx:.1f},'
            f'{self.white_cy:.1f})'
        )

        self.get_logger().info(
            f'Ground offset: '
            f'forward={forward:.3f}m '
            f'right={right:.3f}m'
        )

        self.get_logger().info(
            f'Drone yaw: '
            f'{math.degrees(yaw):.1f} deg'
        )

        self.get_logger().info(
            f'QR GEOTAG: '
            f'x={self.qr_geotag_x:.3f} '
            f'y={self.qr_geotag_y:.3f}'
        )

        self.get_logger().info(
            '=========================================='
        )

        return True


    # ======================================================
    # QR DETECTION
    # ======================================================

    def process_qr(
        self,
        frame,
        log_detection=True
    ):

        if self.qr_confirmed:
            return True


        try:

            rgb = frame[:, :, ::-1]

            image = PILImage.fromarray(
                rgb
            )

            gray = ImageOps.grayscale(
                image
            )

            codes = quirc.decode(
                gray
            )

        except Exception as e:

            self.get_logger().error(
                f'QR decode error: {e}'
            )

            return False


        detected = None


        for code, data in codes:

            payload = (
                data.payload.decode(
                    'utf-8',
                    errors='replace'
                )
            )

            if payload:

                detected = payload.strip()

                break


        if not detected:

            return False


        if detected == self.last_qr:

            self.qr_confirmations += 1

        else:

            self.last_qr = detected

            self.qr_confirmations = 1


        if log_detection:

            self.get_logger().info(
                f'QR candidate: '
                f'{detected} '
                f'({self.qr_confirmations}/'
                f'{QR_CONFIRMATIONS_REQUIRED})'
            )


        if (
            self.qr_confirmations >=
            QR_CONFIRMATIONS_REQUIRED
        ):

            self.qr_value = detected

            self.qr_confirmed = True


            result = String()

            result.data = detected

            self.qr_pub.publish(
                result
            )


            self.get_logger().info(
                f'QR CONFIRMED: '
                f'{detected}'
            )


            return True


        return False


    # ======================================================
    # CENTERING
    # ======================================================

    def execute_centering(self):

        if not self.pose_received:
            return


        altitude = max(
            self.pose.pose.position.z,
            0.5
        )


        if self.filtered_cx is None:

            self.filtered_cx = (
                self.white_cx
            )

            self.filtered_cy = (
                self.white_cy
            )

        else:

            a = CENTER_FILTER_ALPHA

            self.filtered_cx = (
                a * self.white_cx
                +
                (1.0 - a) *
                self.filtered_cx
            )

            self.filtered_cy = (
                a * self.white_cy
                +
                (1.0 - a) *
                self.filtered_cy
            )


        error_x = (
            self.filtered_cx -
            IMAGE_WIDTH / 2.0
        )

        error_y = (
            self.filtered_cy -
            IMAGE_HEIGHT / 2.0
        )


        if (
            abs(error_x) <=
            CENTER_PIXEL_TOLERANCE
            and
            abs(error_y) <=
            CENTER_PIXEL_TOLERANCE
        ):

            self.center_confirmations += 1

        else:

            self.center_confirmations = 0


        if (
            self.center_confirmations >=
            CENTER_CONFIRMATIONS_REQUIRED
        ):

            self.stop_centering()


            self.center_target_x = (
                self.pose.pose.position.x
            )

            self.center_target_y = (
                self.pose.pose.position.y
            )


            self.get_logger().info(
                f'QR TARGET CENTERED '
                f'x={self.center_target_x:.2f} '
                f'y={self.center_target_y:.2f}'
            )


            self.descent_target_z = (
                self.pose.pose.position.z
            )

            self.descent_command_time = 0.0

            self.set_phase(
                'DESCEND'
            )

            return


        now = time.monotonic()

        dt = (
            now -
            self.last_center_time
        )

        if self.last_center_time == 0.0:

            dt = (
                1.0 /
                CENTER_CONTROL_RATE
            )

        if dt < (
            1.0 /
            CENTER_CONTROL_RATE
        ):

            return


        self.last_center_time = now


        ground_x, ground_y = (
            self.image_to_ground_error(
                altitude
            )
        )


        desired_vx = (
            ground_x *
            CENTER_GAIN
        )

        desired_vy = (
            ground_y *
            CENTER_GAIN
        )


        magnitude = math.sqrt(
            desired_vx ** 2 +
            desired_vy ** 2
        )


        if magnitude > MAX_CENTER_SPEED:

            scale = (
                MAX_CENTER_SPEED /
                magnitude
            )

            desired_vx *= scale
            desired_vy *= scale


        max_delta = (
            MAX_CENTER_ACCEL *
            dt
        )


        delta_vx = (
            desired_vx -
            self.center_velocity_x
        )

        delta_vy = (
            desired_vy -
            self.center_velocity_y
        )


        delta_magnitude = math.sqrt(
            delta_vx ** 2 +
            delta_vy ** 2
        )


        if delta_magnitude > max_delta:

            scale = (
                max_delta /
                delta_magnitude
            )

            delta_vx *= scale
            delta_vy *= scale


        self.center_velocity_x += delta_vx
        self.center_velocity_y += delta_vy


        if abs(error_x) < 6:

            self.center_velocity_y = 0.0


        if abs(error_y) < 6:

            self.center_velocity_x = 0.0


        self.publish_center_velocity(
            self.center_velocity_x,
            self.center_velocity_y
        )


        self.get_logger().info(
            f'CENTER '
            f'pixel=('
            f'{error_x:.0f},'
            f'{error_y:.0f}) '
            f'velocity=('
            f'{self.center_velocity_x:.3f},'
            f'{self.center_velocity_y:.3f})'
        )


    # ======================================================
    # DESCENT
    # ======================================================

    def execute_descent(self):

        if not self.pose_received:
            return


        altitude = (
            self.pose.pose.position.z
        )


        self.publish_position_target(
            self.center_target_x,
            self.center_target_y,
            self.descent_target_z
        )


        now = self.elapsed()


        if (
            now -
            self.descent_command_time
        ) >= DESCENT_COMMAND_INTERVAL:

            if (
                self.descent_target_z >
                QR_SEARCH_ALTITUDE
            ):

                self.descent_target_z = max(
                    QR_SEARCH_ALTITUDE,
                    self.descent_target_z -
                    DESCENT_STEP
                )


                self.descent_command_time = now


                self.get_logger().info(
                    f'DESCENT target: '
                    f'{self.descent_target_z:.2f} m '
                    f'actual='
                    f'{altitude:.2f} m'
                )


        if self.qr_confirmed:

            self.get_logger().info(
                f'QR CONFIRMED DURING DESCENT: '
                f'{self.qr_value}'
            )

            self.set_phase(
                'SEND_QR'
            )

            return


        if altitude <= (
            QR_SEARCH_ALTITUDE +
            QR_SEARCH_TOLERANCE
        ):

            self.get_logger().info(
                f'Reached QR search altitude: '
                f'{altitude:.2f} m'
            )

            self.reset_qr_tracking()

            self.set_phase(
                'QR_WAIT'
            )


    # ======================================================
    # CAMERA CALLBACK
    # ======================================================

    def image_callback(self, msg):

        try:

            frame = (
                self.bridge.imgmsg_to_cv2(
                    msg,
                    desired_encoding='bgr8'
                )
            )

        except Exception as e:

            self.get_logger().error(
                f'Camera error: {e}'
            )

            return


        # ==================================================
        # SEARCH
        # ==================================================

        if self.phase == 'SEARCH':

            if self.detect_white_target(frame):

                self.get_logger().info(
                    'WHITE TARGET FOUND DURING SEARCH'
                )

                if not self.pose_received:
                    return


                if self.calculate_geotag():

                    self.filtered_cx = (
                        self.white_cx
                    )

                    self.filtered_cy = (
                        self.white_cy
                    )

                    self.center_confirmations = 0

                    self.center_velocity_x = 0.0
                    self.center_velocity_y = 0.0

                    self.last_center_time = 0.0

                    self.center_target_x = (
                        self.qr_geotag_x
                    )

                    self.center_target_y = (
                        self.qr_geotag_y
                    )

                    self.reset_qr_tracking()

                    self.set_phase(
                        'GO_TO_GEOTAG'
                    )

            return


        # ==================================================
        # GEOTAG NAVIGATION
        # ==================================================

        if self.phase == 'GO_TO_GEOTAG':

            self.process_qr(
                frame,
                log_detection=True
            )

            return


        # ==================================================
        # GEOTAG QR SCAN
        # ==================================================

        if self.phase == 'GEOTAG_SCAN':

            self.process_qr(
                frame,
                log_detection=True
            )

            return


        # ==================================================
        # CENTER
        # ==================================================

        if self.phase == 'CENTER':

            self.detect_white_target(frame)

            return


        # ==================================================
        # DESCENT
        # ==================================================

        if self.phase == 'DESCEND':

            self.process_qr(
                frame,
                log_detection=True
            )

            return


        # ==================================================
        # QR WAIT
        # ==================================================

        if self.phase == 'QR_WAIT':

            self.process_qr(
                frame,
                log_detection=True
            )

            return


    # ======================================================
    # GO TO GEOTAG
    # ======================================================

    def execute_geotag_navigation(self):

        if not self.geotag_valid:

            self.get_logger().error(
                'No valid geotag'
            )

            self.set_phase('RTL')

            return


        if not self.pose_received:
            return


        self.publish_position_target(
            self.qr_geotag_x,
            self.qr_geotag_y,
            TAKEOFF_ALTITUDE
        )


        dx = (
            self.pose.pose.position.x -
            self.qr_geotag_x
        )

        dy = (
            self.pose.pose.position.y -
            self.qr_geotag_y
        )


        distance = math.sqrt(
            dx * dx +
            dy * dy
        )


        if distance <= GEOTAG_TOLERANCE:

            self.get_logger().info(
                f'REACHED QR GEOTAG '
                f'x={self.qr_geotag_x:.3f} '
                f'y={self.qr_geotag_y:.3f}'
            )

            self.get_logger().info(
                'Trying QR scan at geotag position'
            )

            self.reset_qr_tracking()

            self.set_phase(
                'GEOTAG_SCAN'
            )


    # ======================================================
    # GEOTAG SCAN
    # ======================================================

    def execute_geotag_scan(self):

        if not self.pose_received:
            return


        self.publish_position_target(
            self.qr_geotag_x,
            self.qr_geotag_y,
            TAKEOFF_ALTITUDE
        )


        if self.qr_confirmed:

            self.get_logger().info(
                'QR SCANNED DIRECTLY AT GEOTAG'
            )

            self.set_phase(
                'SEND_QR'
            )

            return


        if self.elapsed() >= QR_GEOTAG_SCAN_TIME:

            self.get_logger().warn(
                'QR SCAN FAILED AT GEOTAG'
            )

            self.get_logger().info(
                'Starting visual centering before descent'
            )

            self.reset_qr_tracking()

            self.filtered_cx = None
            self.filtered_cy = None

            self.center_confirmations = 0

            self.center_velocity_x = 0.0
            self.center_velocity_y = 0.0

            self.last_center_time = 0.0

            self.center_target_x = (
                self.qr_geotag_x
            )

            self.center_target_y = (
                self.qr_geotag_y
            )

            # CENTER now happens at the current altitude.
            # Once centered, CENTER transitions to DESCEND.
            self.set_phase(
                'CENTER'
            )


    # ======================================================
    # UDP
    # ======================================================

    def send_qr_to_gcs(self):

        if not self.qr_confirmed:

            self.get_logger().error(
                'UDP SEND ABORTED: QR not confirmed'
            )

            return False


        payload_text = (
            self.qr_value.strip()
        )

        if not payload_text:

            self.get_logger().error(
                'UDP SEND ABORTED: empty payload'
            )

            return False


        payload = (
            payload_text.encode('utf-8')
        )


        self.get_logger().info(
            f'SENDING QR TO GCS: '
            f'{payload_text}'
        )

        self.get_logger().info(
            f'UDP destination: '
            f'{GCS_IP}:{GCS_PORT}'
        )


        try:

            for i in range(
                UDP_SEND_COUNT
            ):

                sent = (
                    self.udp_socket.sendto(
                        payload,
                        (
                            GCS_IP,
                            GCS_PORT
                        )
                    )
                )


                self.get_logger().info(
                    f'GCS UDP SENT '
                    f'{i + 1}/'
                    f'{UDP_SEND_COUNT} '
                    f'({sent} bytes): '
                    f'{payload_text}'
                )


                if i < (
                    UDP_SEND_COUNT - 1
                ):

                    time.sleep(
                        UDP_SEND_INTERVAL
                    )


            self.udp_sent = True


            self.get_logger().info(
                'QR DATA SENT SUCCESSFULLY'
            )


            return True


        except Exception as e:

            self.get_logger().error(
                f'GCS UDP ERROR: {e}'
            )

            return False


    # ======================================================
    # RTL
    # ======================================================

    def send_rtl(self):

        if not self.mode_client.service_is_ready():
            return


        req = SetMode.Request()

        req.base_mode = 0
        req.custom_mode = 'RTL'


        future = (
            self.mode_client.call_async(req)
        )


        future.add_done_callback(
            self.rtl_response
        )


    def rtl_response(self, future):

        try:

            response = future.result()

            self.get_logger().info(
                f'RTL command: '
                f'mode_sent={response.mode_sent}'
            )

        except Exception as e:

            self.get_logger().error(
                f'RTL error: {e}'
            )


    # ======================================================
    # MISSION LOOP
    # ======================================================

    def mission_loop(self):

        # ==================================================
        # WAIT FCU
        # ==================================================

        if self.phase == 'WAIT_FCU':

            if self.state.connected:

                self.get_logger().info(
                    'FCU connected'
                )

                self.configure_search_motion()

                self.set_phase(
                    'WAIT_START'
                )

            return


        # ==================================================
        # WAIT FOR USER START
        # ==================================================

        if self.phase == 'WAIT_START':

            if not self.start_prompted:

                self.start_prompted = True

                threading.Thread(
                    target=self.wait_for_start_confirmation,
                    daemon=True
                ).start()

            if self.start_event.is_set():

                self.get_logger().info(
                    '=========================================='
                )

                self.get_logger().info(
                    'STARTING AUTONOMOUS MISSION'
                )

                self.get_logger().info(
                    '=========================================='
                )

                self.set_phase(
                    'GUIDED'
                )

            return


        # ==================================================
        # GUIDED
        # ==================================================

        if self.phase == 'GUIDED':

            if self.state.mode != 'GUIDED':

                if self.elapsed() >= 1.0:

                    self.set_guided()

                    self.phase_start = (
                        self.get_clock().now()
                    )

            else:

                self.get_logger().info(
                    'GUIDED mode active'
                )

                self.set_phase(
                    'ARM'
                )

            return


        # ==================================================
        # ARM
        # ==================================================

        if self.phase == 'ARM':

            if not self.state.armed:

                if self.elapsed() >= 1.0:

                    self.arm()

                    self.phase_start = (
                        self.get_clock().now()
                    )

            else:

                if not self.pose_received:
                    return


                self.get_logger().info(
                    'Vehicle armed'
                )


                self.search_origin_x = (
                    self.pose.pose.position.x
                )

                self.search_origin_y = (
                    self.pose.pose.position.y
                )


                self.get_logger().info(
                    f'Start position: '
                    f'x={self.search_origin_x:.2f} '
                    f'y={self.search_origin_y:.2f}'
                )


                self.set_phase(
                    'TAKEOFF'
                )

            return


        # ==================================================
        # TAKEOFF
        # ==================================================

        if self.phase == 'TAKEOFF':

            if not self.pose_received:
                return


            altitude = (
                self.pose.pose.position.z
            )


            if not self.takeoff_sent:

                if self.elapsed() >= 0.5:

                    self.get_logger().info(
                        'Sending '
                        'MAV_CMD_NAV_TAKEOFF '
                        f'to {TAKEOFF_ALTITUDE:.1f} m'
                    )

                    self.send_takeoff()

                    self.phase_start = (
                        self.get_clock().now()
                    )

                return


            if altitude >= (
                TAKEOFF_ALTITUDE -
                TAKEOFF_TOLERANCE
            ):

                self.get_logger().info(
                    f'Takeoff complete: '
                    f'{altitude:.2f} m'
                )


                self.build_search_pattern()


                self.set_phase(
                    'SEARCH'
                )

            return


        # ==================================================
        # SEARCH
        # ==================================================

        if self.phase == 'SEARCH':

            self.execute_search()

            return


        # ==================================================
        # GO TO GEOTAG
        # ==================================================

        if self.phase == 'GO_TO_GEOTAG':

            self.execute_geotag_navigation()

            return


        # ==================================================
        # GEOTAG SCAN
        # ==================================================

        if self.phase == 'GEOTAG_SCAN':

            self.execute_geotag_scan()

            return


        # ==================================================
        # CENTER
        # ==================================================

        if self.phase == 'CENTER':

            self.execute_centering()

            return


        # ==================================================
        # DESCEND
        # ==================================================

        if self.phase == 'DESCEND':

            self.execute_descent()

            return


        # ==================================================
        # QR WAIT
        # ==================================================

        if self.phase == 'QR_WAIT':

            self.publish_position_target(
                self.center_target_x,
                self.center_target_y,
                QR_SEARCH_ALTITUDE
            )


            if self.qr_confirmed:

                self.get_logger().info(
                    f'QR READY FOR GCS: '
                    f'{self.qr_value}'
                )

                self.set_phase(
                    'SEND_QR'
                )

                return


            if self.elapsed() >= 1.0:

                self.get_logger().info(
                    f'Searching for QR... '
                    f'altitude='
                    f'{self.pose.pose.position.z:.2f} m '
                    f'time='
                    f'{self.elapsed():.1f}/'
                    f'{QR_WAIT_TIMEOUT:.1f}s'
                )


                # IMPORTANT:
                # Previously this phase waited forever.
                # Now it returns to the search pattern.
                if self.elapsed() >= QR_WAIT_TIMEOUT:

                    self.get_logger().warn(
                        'QR NOT FOUND AT LOW ALTITUDE'
                    )

                    self.resume_search()


            return


        # ==================================================
        # SEND QR
        # ==================================================

        if self.phase == 'SEND_QR':

            if not self.udp_sent:

                if self.send_qr_to_gcs():

                    self.set_phase(
                        'RTL'
                    )

            else:

                self.set_phase(
                    'RTL'
                )

            return


        # ==================================================
        # RTL
        # ==================================================

        if self.phase == 'RTL':

            if not self.rtl_sent:

                self.get_logger().info(
                    'SWITCHING TO RTL'
                )

                self.send_rtl()

                self.rtl_sent = True

                self.set_phase(
                    'DONE'
                )

            return


        # ==================================================
        # DONE
        # ==================================================

        if self.phase == 'DONE':
            return


def main(args=None):

    rclpy.init(args=args)

    node = GeoTagMission()

    try:

        rclpy.spin(node)

    except KeyboardInterrupt:

        pass

    finally:

        try:
            node.stop_centering()
        except Exception:
            pass

        try:
            node.udp_socket.close()
        except Exception:
            pass

        node.destroy_node()

        if rclpy.ok():

            rclpy.shutdown()


if __name__ == '__main__':

    main()
