import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

from geometry_msgs.msg import PoseStamped
from mavros_msgs.msg import State
from mavros_msgs.srv import CommandBool, SetMode


class TakeoffSetpointTest(Node):

    def __init__(self):
        super().__init__('takeoff_setpoint_test')

        pose_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT
        )

        self.pub = self.create_publisher(
            PoseStamped,
            '/mavros/setpoint_position/local',
            10
        )

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
            pose_qos
        )

        self.arm_client = self.create_client(
            CommandBool,
            '/mavros/cmd/arming'
        )

        self.mode_client = self.create_client(
            SetMode,
            '/mavros/set_mode'
        )

        self.state = State()
        self.pose = PoseStamped()

        self.start_time = time.monotonic()
        self.last_log = 0

        self.guided_sent = False
        self.arm_sent = False

        self.create_timer(0.05, self.update)

    def state_callback(self, msg):
        self.state = msg

    def pose_callback(self, msg):
        self.pose = msg

    def publish(self):
        msg = PoseStamped()

        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'map'

        msg.pose.position.x = 0.0
        msg.pose.position.y = 0.0
        msg.pose.position.z = 10.0

        msg.pose.orientation.w = 1.0

        self.pub.publish(msg)

    def update(self):

        self.publish()

        now = time.monotonic()

        if now - self.last_log >= 1.0:

            self.last_log = now

            self.get_logger().info(
                f'MODE={self.state.mode} '
                f'ARMED={self.state.armed} '
                f'POS=('
                f'{self.pose.pose.position.x:.2f}, '
                f'{self.pose.pose.position.y:.2f}, '
                f'{self.pose.pose.position.z:.2f})'
            )

        if not self.state.connected:
            return

        if self.state.mode != 'GUIDED' and not self.guided_sent:

            if self.mode_client.service_is_ready():

                req = SetMode.Request()
                req.base_mode = 0
                req.custom_mode = 'GUIDED'

                future = self.mode_client.call_async(req)
                future.add_done_callback(self.guided_response)

                self.guided_sent = True

                self.get_logger().info(
                    'GUIDED request sent'
                )

            return

        if self.state.mode != 'GUIDED':
            return

        if not self.state.armed and not self.arm_sent:

            if self.arm_client.service_is_ready():

                req = CommandBool.Request()
                req.value = True

                future = self.arm_client.call_async(req)
                future.add_done_callback(self.arm_response)

                self.arm_sent = True

                self.get_logger().info(
                    'ARM request sent'
                )

    def guided_response(self, future):

        try:
            response = future.result()

            self.get_logger().info(
                f'GUIDED response: {response.mode_sent}'
            )

        except Exception as e:
            self.get_logger().error(str(e))

    def arm_response(self, future):

        try:
            response = future.result()

            self.get_logger().info(
                f'ARM response: '
                f'success={response.success} '
                f'result={response.result}'
            )

        except Exception as e:
            self.get_logger().error(str(e))


def main(args=None):

    rclpy.init(args=args)

    node = TakeoffSetpointTest()

    try:
        rclpy.spin(node)

    except KeyboardInterrupt:
        print('\nTest stopped.')

    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
