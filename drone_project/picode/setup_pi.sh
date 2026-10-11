#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
readonly ROS_DISTRO_NAME="humble"
readonly BOOT_CONFIG="/boot/firmware/config.txt"

log() {
    printf '[picode setup] %s\n' "$*"
}

die() {
    printf '[picode setup] ERROR: %s\n' "$*" >&2
    exit 1
}

if [[ ${EUID} -eq 0 ]]; then
    die "Run this as the normal Pi user, not root; the script uses sudo when needed."
fi

[[ -r /etc/os-release ]] || die "Cannot identify the operating system."
# shellcheck source=/dev/null
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 22.04 ]] || \
    die "This installer supports Ubuntu 22.04 only (found ${PRETTY_NAME:-unknown})."
[[ $(uname -m) == aarch64 ]] || \
    die "A 64-bit Raspberry Pi OS install is required (found $(uname -m))."
command -v sudo >/dev/null || die "sudo is required."
sudo -v

if [[ -r /proc/device-tree/model ]]; then
    pi_model="$(tr -d '\0' </proc/device-tree/model)"
    [[ ${pi_model} == *"Raspberry Pi 4"* ]] || \
        die "Expected a Raspberry Pi 4, found: ${pi_model}"
else
    die "This does not appear to be a Raspberry Pi."
fi

log "Installing Ubuntu build, Python, video, tmux, and serial prerequisites"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    git \
    locales \
    pkg-config \
    python3-dev \
    python3-lxml \
    python3-matplotlib \
    python3-numpy \
    python3-opencv \
    python3-pil \
    python3-pip \
    python3-venv \
    software-properties-common \
    tmux

sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
sudo add-apt-repository -y universe

if [[ ! -f /etc/apt/sources.list.d/ros2.sources && \
      ! -f /etc/apt/sources.list.d/ros2.list ]]; then
    log "Adding the official ROS 2 apt source"
    ros_apt_version="$({
        curl -fsSL \
            https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest
    } | sed -n 's/.*"tag_name": "\([^"]*\)".*/\1/p' | head -n 1)"
    [[ -n ${ros_apt_version} ]] || die "Could not determine ros2-apt-source version."
    readonly ros_apt_deb="/tmp/ros2-apt-source-${ros_apt_version}.deb"
    curl -fL \
        "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ros_apt_version}/ros2-apt-source_${ros_apt_version}.${UBUNTU_CODENAME}_all.deb" \
        -o "${ros_apt_deb}"
    sudo dpkg -i "${ros_apt_deb}"
fi

log "Installing ROS 2 Humble, MAVROS, cv_bridge, and build tools"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ros-dev-tools \
    ros-humble-cv-bridge \
    ros-humble-image-transport \
    ros-humble-mavros \
    ros-humble-mavros-extras \
    ros-humble-ros-base

readonly geo_script="/opt/ros/${ROS_DISTRO_NAME}/lib/mavros/install_geographiclib_datasets.sh"
[[ -x ${geo_script} ]] || die "MAVROS GeographicLib installer was not found."
if [[ ! -f /usr/share/GeographicLib/geoids/egm96-5.pgm ]]; then
    log "Installing mandatory MAVROS GeographicLib datasets"
    sudo "${geo_script}"
fi

log "Creating the project virtual environment"
python3 -m venv --system-site-packages "${SCRIPT_DIR}/.venv"
"${SCRIPT_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel
"${SCRIPT_DIR}/.venv/bin/python" -m pip install \
    --requirement "${SCRIPT_DIR}/requirements.txt"

log "Building the ROS 2 workspace"
# shellcheck source=/dev/null
source "/opt/ros/${ROS_DISTRO_NAME}/setup.bash"
cd "${PROJECT_DIR}/ros2_ws"
colcon build --symlink-install --packages-select rescue_control

log "Granting ${USER} serial-port access"
sudo usermod --append --groups dialout "${USER}"

[[ -f ${BOOT_CONFIG} ]] || die "Ubuntu Pi boot config not found at ${BOOT_CONFIG}."
if ! grep -Eq '^[[:space:]]*dtoverlay=uart3([,[:space:]]|$)' "${BOOT_CONFIG}"; then
    log "Enabling UART3 (GPIO4/GPIO5; expected device /dev/ttyAMA2)"
    uart_block="$(mktemp)"
    printf '\n# BEGIN drone_project UART3\ndtoverlay=uart3\n# END drone_project UART3\n' \
        >"${uart_block}"
    sudo cp --archive --no-clobber \
        "${BOOT_CONFIG}" "${BOOT_CONFIG}.pre-drone-project"
    # The input file is user-readable; sudo is needed only for the tee target.
    # shellcheck disable=SC2024
    sudo tee -a "${BOOT_CONFIG}" <"${uart_block}" >/dev/null
    rm -f -- "${uart_block}"
else
    log "UART3 overlay already present"
fi

# Ensure no login console can take ownership if that unit was enabled manually.
sudo systemctl disable --now serial-getty@ttyAMA2.service >/dev/null 2>&1 || true
if [[ -r /boot/firmware/cmdline.txt ]] && \
   grep -Eq '(^|[[:space:]])console=ttyAMA2([,[:space:]]|$)' \
       /boot/firmware/cmdline.txt; then
    die "ttyAMA2 is still a kernel console in /boot/firmware/cmdline.txt; remove only its console=ttyAMA2,... token, then reboot."
fi

mkdir -p "${SCRIPT_DIR}/logs"
chmod +x "${SCRIPT_DIR}/geotag_mission.py" "${SCRIPT_DIR}/testforward.py"

log "Running software import checks"
"${SCRIPT_DIR}/.venv/bin/python" -c \
    'import cv2, numpy, PIL, MAVProxy, pymavlink, quirc, yaml; print("Python imports: PASS")'
bash -c \
    "source /opt/ros/${ROS_DISTRO_NAME}/setup.bash && source '${PROJECT_DIR}/ros2_ws/install/setup.bash' && '${SCRIPT_DIR}/.venv/bin/python' -c 'import cv_bridge, mavros_msgs, rclpy; from rescue_control.geotag_mission import GeoTagMission; print(\"ROS/mission imports: PASS\")'"

log "Installation complete. Reboot before UART use: sudo reboot"
log "After reconnecting, follow ${SCRIPT_DIR}/README.md from 'Post-reboot checks'."
