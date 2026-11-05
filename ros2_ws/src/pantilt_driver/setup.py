from glob import glob

from setuptools import setup

package_name = "pantilt_driver"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Polar",
    maintainer_email="aklex1978@gmail.com",
    description="Polar: керування двома сервоприводами ST3020 камери",
    license="Proprietary",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "pantilt_node = pantilt_driver.node:main",
        ],
    },
)
