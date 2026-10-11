import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
import cv2
import numpy as np


class CameraView(Node):

    def __init__(self):
        super().__init__('camera_view')

        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
            reliability=ReliabilityPolicy.BEST_EFFORT
        )

        self.bridge = CvBridge()

        self.front = None
        self.down = None

        self.create_subscription(
            Image,
            '/camera/front/image',
            self.front_callback,
            qos
        )

        self.create_subscription(
            Image,
            '/camera/down/image',
            self.down_callback,
            qos
        )

        self.timer = self.create_timer(
            0.03,
            self.display
        )

    def front_callback(self, msg):
        self.front = self.bridge.imgmsg_to_cv2(
            msg,
            desired_encoding='bgr8'
        )

    def down_callback(self, msg):
        self.down = self.bridge.imgmsg_to_cv2(
            msg,
            desired_encoding='bgr8'
        )

    def display(self):

        if self.front is None:
            front = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(
                front,
                'WAITING FOR FRONT CAMERA',
                (80, 180),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (255, 255, 255),
                2
            )
        else:
            front = self.front.copy()

        if self.down is None:
            down = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(
                down,
                'WAITING FOR DOWN CAMERA',
                (70, 180),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (255, 255, 255),
                2
            )
        else:
            down = self.down.copy()

        front = cv2.resize(front, (640, 360))
        down = cv2.resize(down, (640, 360))

        combined = np.hstack((front, down))

        cv2.putText(
            combined,
            'FRONT',
            (20, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2
        )

        cv2.putText(
            combined,
            'DOWN',
            (660, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2
        )

        cv2.imshow(
            'ADDC SIM - Drone Cameras',
            combined
        )

        cv2.waitKey(1)


def main():

    rclpy.init()

    node = CameraView()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass

    node.destroy_node()

    if rclpy.ok():
        rclpy.shutdown()

    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
