from setuptools import find_packages, setup

package_name = 'rescue_control'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        (
            'share/ament_index/resource_index/packages',
            ['resource/' + package_name]
        ),
        (
            'share/' + package_name,
            ['package.xml']
        ),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='user',
    maintainer_email='user@todo.todo',
    description='Autonomous rescue drone control',
    license='TODO',
    entry_points={
        'console_scripts': [
            'autonomous_mission = rescue_control.autonomous_mission:main',
            'geotag_mission = rescue_control.geotag_mission:main',
		'qr_detector = rescue_control.qr_detector:main',
        ],
    },
)
