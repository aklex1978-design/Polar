import os
import sys

# Дозволяє запускати тести простим pytest, без colcon і без ROS.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
