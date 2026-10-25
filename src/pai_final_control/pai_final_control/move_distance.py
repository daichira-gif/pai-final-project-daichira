#!/usr/bin/env python3

import math
import time
import sys

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry


class MoveDistance(Node):
    def __init__(self):
        super().__init__('move_distance')

        self.declare_parameter('distance', 0.5)
        self.declare_parameter('speed', 0.06)
        self.declare_parameter('timeout', 20.0)

        self.target = float(
            self.get_parameter('distance').value
        )
        self.speed = float(
            self.get_parameter('speed').value
        )
        self.timeout = float(
            self.get_parameter('timeout').value
        )

        if not (
            math.isfinite(self.target)
            and 0.0 < self.target <= 1.0
            and math.isfinite(self.speed)
            and 0.0 < self.speed <= 0.1
            and math.isfinite(self.timeout)
            and 0.0 < self.timeout <= 60.0
        ):
            raise ValueError('Invalid motion parameters')

        self.publisher = self.create_publisher(
            Twist, '/cmd_vel', 10
        )

        self.subscription = self.create_subscription(
            Odometry, '/odom', self.odom_callback, 10
        )

        self.start_pose = None
        self.current_pose = None
        self.start_yaw = None

        self.started_at = None
        self.last_odom_at = None
        self.created_at = time.monotonic()
        self.exit_code = 1

        self.finished = False
        self.stop_reason = None
        self.last_log_at = 0.0

        self.timer = self.create_timer(
            0.1, self.control_loop
        )

        self.get_logger().info(
            f'Ready: distance={self.target:.2f} m, '
            f'speed={self.speed:.2f} m/s'
        )

    def odom_callback(self, msg):
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation

        yaw = math.atan2(
            2.0 * (q.w * q.z + q.x * q.y),
            1.0 - 2.0 * (q.y * q.y + q.z * q.z)
        )

        self.current_pose = (p.x, p.y)
        self.last_odom_at = time.monotonic()

        if self.start_pose is None:
            self.start_pose = self.current_pose
            self.start_yaw = yaw
            self.started_at = time.monotonic()

            self.get_logger().info(
                'Initial odometry received'
            )

    def publish_velocity(self, linear=0.0):
        msg = Twist()
        msg.linear.x = float(linear)
        self.publisher.publish(msg)

    def stop(self, reason):
        if self.finished:
            return

        self.finished = True
        self.stop_reason = reason
        self.exit_code = (
            0 if reason.startswith('TARGET_REACHED') else 1
        )
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

        dx = self.current_pose[0] - self.start_pose[0]
        dy = self.current_pose[1] - self.start_pose[1]

        # Project displacement onto initial heading.
        distance = (
            dx * math.cos(self.start_yaw)
            + dy * math.sin(self.start_yaw)
        )

        remaining = self.target - distance

        if remaining <= 0.01:
            self.stop(
                f'TARGET_REACHED distance={distance:.3f}m'
            )
            return

        # Reduce speed near the target.
        command_speed = min(
            self.speed,
            max(0.015, 0.7 * remaining)
        )

        self.publish_velocity(command_speed)

        if now - self.last_log_at >= 1.0:
            self.get_logger().info(
                f'distance={distance:.3f}m '
                f'remaining={remaining:.3f}m'
            )
            self.last_log_at = now

    def destroy_node(self):
        # Publish zero velocity repeatedly before exit.
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
            node = MoveDistance()
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
