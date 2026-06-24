#!/bin/bash
sudo rmmod i2c_designware_platform i2c_designware_core && sleep 2 && sudo modprobe i2c_designware_platform
