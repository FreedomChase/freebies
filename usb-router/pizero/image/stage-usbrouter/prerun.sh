#!/bin/bash -e
# pi-gen: start this stage from stage2's root filesystem (Raspberry Pi OS Lite)
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
