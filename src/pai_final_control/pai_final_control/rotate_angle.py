#!/usr/bin/env python3

import math
import sys
import time

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry


def normalize_angle(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


class RotateAngle(Node):

    def __init__(self):
        super().__init__('rotate_angle')

        self.declare_parameter('angle_deg', 90.0)
        self.declare_parameter('max_speed', 0.3)
        self.declare_parameter('timeout', 20.0)

        angle_deg = float(
            self.get_parameter('angle_deg').value
        )
        self.max_speed = float(
            self.get_parameter('max_speed').value
        )
        self.timeout = float(
            self.get_parameter('timeout').value
        )

        if not (
            math.isfinite(angle_deg)
            and 0.0 < abs(angle_deg) <= 180.0
            and math.isfinite(self.max_speed)
            and 0.0 < self.max_speed <= 0.5
            and math.isfinite(self.timeout)
            and 0.0 < self.timeout <= 60.0
        ):
            raise ValueError('Invalid rotation parameters')

        self.target_angle = math.radians(angle_deg)

        self.publisher = self.create_publisher(
            Twist, '/cmd_vel', 10
        )

        self.subscription = self.create_subscription(
            Odometry, '/odom', self.odom_callback, 10
        )

        self.created_at = time.monotonic()
        self.started_at = None
        self.last_odom_at = None

        self.previous_yaw = None
        self.accumulated_angle = 0.0

        self.finished = False
        self.exit_code = 1
        self.last_log_at = 0.0

        self.timer = self.create_timer(
            0.1, self.control_loop
        )

        self.get_logger().info(
            f'Ready: angle={angle_deg:.1f} deg, '
            f'max_speed={self.max_speed:.2f} rad/s'
        )

    def odom_callback(self, msg):
        q = msg.pose.pose.orientation

        yaw = math.atan2(
            2.0 * (q.w * q.z + q.x * q.y),
            1.0 - 2.0 * (q.y * q.y + q.z * q.z)
        )

        now = time.monotonic()

        if self.finished:
            return

        if self.previous_yaw is None:
            self.previous_yaw = yaw
            self.started_at = now
            self.get_logger().info(
                'Initial odometry received'
            )
        else:
            delta = normalize_angle(
                yaw - self.previous_yaw
            )
            self.accumulated_angle += delta
            self.previous_yaw = yaw

        self.last_odom_at = now

    def publish_velocity(self, angular=0.0):
        msg = Twist()
        msg.angular.z = float(angular)
        self.publisher.publish(msg)

    def stop(self, reason, success=False):
        if self.finished:
            return

        self.finished = True
        self.exit_code = 0 if success else 1
        self.publish_velocity(0.0)

        self.get_logger().info(
            f'STOP: {reason}'
        )

    def control_loop(self):
        if self.finished:
            self.publish_velocity(0.0)
            return

        now = time.monotonic()

        if self.started_at is None:
            self.publish_velocity(0.0)

            if now - self.created_at > 5.0:
                self.stop('INITIAL_ODOMETRY_TIMEOUT')

            return

        if now - self.started_at > self.timeout:
            self.stop('TIMEOUT')
            return

        if (
            self.last_odom_at is None
            or now - self.last_odom_at > 0.5
        ):
            self.stop('ODOMETRY_LOST')
            return

        error = (
            self.target_angle
            - self.accumulated_angle
        )

        tolerance = math.radians(1.5)

        if abs(error) <= tolerance:
            self.stop(
                'TARGET_REACHED '
                f'angle={math.degrees(self.accumulated_angle):.2f}deg',
                success=True
            )
            return

        speed = min(
            self.max_speed,
            max(0.05, 1.2 * abs(error))
        )

        direction = 1.0 if error > 0 else -1.0

        self.publish_velocity(
            direction * speed
        )

        if now - self.last_log_at >= 1.0:
            self.get_logger().info(
                f'angle={math.degrees(self.accumulated_angle):.2f}deg '
                f'error={math.degrees(error):.2f}deg'
            )
            self.last_log_at = now

    def destroy_node(self):
        for _ in range(5):
            self.publish_velocity(0.0)
            time.sleep(0.05)

        super().destroy_node()


def main(args=None):
    import signal
    import traceback
    from rclpy.node import Node as RosNode
    from rclpy.signals import SignalHandlerOptions

    request = {"signum": None, "count": 0}
    previous_handlers = {}
    node = None
    initialized = False
    exit_code = 1
    cleanup_failed = False

    def record_signal(signum, frame):
        if request["signum"] is None:
            request["signum"] = signum
        request["count"] += 1

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, record_signal)
        # Keep the ROS context alive until zero velocity and node cleanup finish.
        rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
        initialized = True

        if request["signum"] is None:
            node = RotateAngle()
            while rclpy.ok() and not node.finished and request["signum"] is None:
                rclpy.spin_once(node, timeout_sec=0.1)

        if request["signum"] is not None:
            if node is not None and rclpy.ok():
                node.stop("SIGNAL_" + signal.Signals(request["signum"]).name)
            exit_code = 128 + request["signum"]
        elif node is not None and rclpy.ok():
            exit_code = node.exit_code
        else:
            print("NODE_ERROR: ROS context unavailable", flush=True)

    except Exception:
        print("NODE_ERROR: exception during execution or stop", flush=True)
        traceback.print_exc()
        exit_code = 1

    finally:
        # Repeated SIGINT/SIGTERM continue to record state during this cleanup.
        if node is not None:
            try:
                if rclpy.ok():
                    node.destroy_node()
                    print("NODE_CLEANUP_OK: zero-publish cleanup completed", flush=True)
                else:
                    cleanup_failed = True
                    print("NODE_CLEANUP_ERROR: context invalid; zero publish skipped", flush=True)
                    RosNode.destroy_node(node)
            except Exception:
                cleanup_failed = True
                print("NODE_CLEANUP_ERROR: destroy_node failed", flush=True)
                traceback.print_exc()
                try:
                    # Release resources without invoking subclass zero-publish again.
                    RosNode.destroy_node(node)
                except Exception:
                    print("NODE_CLEANUP_ERROR: base destroy failed", flush=True)
                    traceback.print_exc()

        try:
            if initialized and rclpy.ok():
                rclpy.shutdown()
        except Exception:
            cleanup_failed = True
            print("NODE_CLEANUP_ERROR: shutdown failed", flush=True)
            traceback.print_exc()
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)

    if request["signum"] is not None:
        print("NODE_INTERRUPTED: " + signal.Signals(request["signum"]).name
              + " SIGNAL_COUNT=" + str(request["count"]), flush=True)
        exit_code = 128 + request["signum"]
    if cleanup_failed:
        print("NODE_CLEANUP_FAILED", flush=True)
        if exit_code == 0:
            exit_code = 1
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
