"""Запуск pantilt_driver для однієї камери: ros2 launch pantilt_driver pantilt.launch.py camera:=camera_1 backend:=sim"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def generate_launch_description():
    share = get_package_share_directory("pantilt_driver")
    camera = LaunchConfiguration("camera")
    backend = LaunchConfiguration("backend")
    params = LaunchConfiguration("params")
    return LaunchDescription([
        DeclareLaunchArgument("camera", default_value="camera_1"),
        DeclareLaunchArgument("backend", default_value="sim", description="sim | st3020"),
        DeclareLaunchArgument("params", default_value=os.path.join(share, "config", "camera_1.yaml")),
        Node(
            package="pantilt_driver",
            executable="pantilt_node",
            namespace=PythonExpression(["'/polar/' + '", camera, "'"]),
            parameters=[params, {"backend": backend}],
            output="screen",
        ),
    ])
