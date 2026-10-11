import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

from sensor_msgs.msg import Image
from std_msgs.msg import String

from cv_bridge import CvBridge
from PIL import Image as PILImage
from PIL import ImageOps

import quirc
import socket


# ==========================================================
# GCS UDP SETTINGS
# ==========================================================

GCS_IP = "127.0.0.1"
GCS_PORT = 5005


class QRDetector(Node):

    def __init__(self):
        super().__init__('qr_detector')

        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
            reliability=ReliabilityPolicy.BEST_EFFORT
        )

        self.bridge = CvBridge()

        self.last_data = ""
        self.confirmations = 0
        self.required_confirmations = 5
        self.confirmed = False

        # UDP socket
        self.udp_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM
        )

        # Downward camera
        self.image_sub = self.create_subscription(
            Image,
            '/camera/down/image',
            self.image_callback,
            qos
        )

        # ROS QR result
        self.result_pub = self.create_publisher(
            String,
            '/rescue/qr_detected',
            10
        )

        self.get_logger().info(
            f'QR detector started. GCS UDP: '
            f'{GCS_IP}:{GCS_PORT}'
        )

    # ======================================================
    # SEND QR DATA TO GCS
    # ======================================================

    def send_to_gcs(self, data):

        message = f'QR FOUND: {data}'

        try:
            self.udp_socket.sendto(
                message.encode('utf-8'),
                (GCS_IP, GCS_PORT)
            )

            self.get_logger().info(
                f'GCS UDP sent: {message}'
            )

        except Exception as e:

            self.get_logger().error(
                f'UDP send error: {e}'
            )

    # ======================================================
    # CAMERA CALLBACK
    # ======================================================

    def image_callback(self, msg):

        if self.confirmed:
            return

        try:

            frame = self.bridge.imgmsg_to_cv2(
                msg,
                desired_encoding='bgr8'
            )

            rgb = frame[:, :, ::-1]

            image = PILImage.fromarray(rgb)

            gray = ImageOps.grayscale(image)

            codes = quirc.decode(gray)

        except Exception as e:

            self.get_logger().error(
                f'QR decode error: {e}'
            )

            return

        detected_data = None

        for code, data in codes:

            payload = data.payload.decode(
                'utf-8',
                errors='replace'
            )

            if payload:
                detected_data = payload
                break

        if detected_data is None:
            return

        # ==================================================
        # FIVE CONSECUTIVE CONFIRMATIONS
        # ==================================================

        if detected_data == self.last_data:

            self.confirmations += 1

        else:

            self.last_data = detected_data
            self.confirmations = 1

        self.get_logger().info(
            f'QR candidate: {detected_data} '
            f'({self.confirmations}/'
            f'{self.required_confirmations})'
        )

        # ==================================================
        # CONFIRMED
        # ==================================================

        if self.confirmations >= self.required_confirmations:

            # ROS topic
            result = String()
            result.data = detected_data
            self.result_pub.publish(result)

            # Direct Wi-Fi / UDP to GCS
            self.send_to_gcs(detected_data)

            self.confirmed = True

            self.get_logger().info(
                f'QR CONFIRMED: {detected_data}'
            )


def main():

    rclpy.init()

    node = QRDetector()

    try:
        rclpy.spin(node)

    except KeyboardInterrupt:
        pass

    node.destroy_node()

    rclpy.shutdown()


if __name__ == '__main__':
    main()
