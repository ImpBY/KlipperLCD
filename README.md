# KlipperLCD (for Elegoo Neptune 3 Pro LCD screen)
Want to run Klipper on your Neptune 3 Pro? And still want to be able to use your Neptune 3 Pro LCD touch screen?

Take a look at this python service for the Elegoo Neptune 3 Pro LCD! Running together with Klipper3d and Moonraker!

## Look and feel
<p float="left">
    <img src="img/boot_screen.PNG" height="400">
    <img src="img/main_screen.PNG" height="400">
    <img src="img/about_screen.PNG" height="400">
</p>

## Whats needed?
* A Elegoo Neptune 3 Pro with LCD screen.
* A Raspberry Pi or similar SBC to run Klipper. I suggest using the [Klipper Installation And Update Helper (KIAUH)](https://github.com/dw-0/kiauh) to setup and install Klipper, Moonraker and the web user interface of choice ([Fluidd](https://docs.fluidd.xyz/)/[Mainsail](https://docs.mainsail.xyz/)).
* Some re-wiring of the LCD screen to connect it to one of the UARTs availible on your Raspberry Pi / SBC or through a USB to UART converter.
* Then you can follow this guide to enable your Neptune 3 Pro touch screen!

## Wire the LCD
When wiring your screen, you can either wire it directly to one of your Raspberry Pi / SBC availible UARTs or you can wire it through a USB to UART converter. Both options are described below, pick the option that suits your needs.

### To a Raspberry Pi UART
1. Remove the back-cover of the LCD by unscrewing the four screws.

2. Connect the LCD to the Raspberry Pi UART according to the table below:

    | Raspberry Pi  | LCD               |
    | ------------- | ----------------- |
    | Pin 4 (5V)    | 5V  (Black wire)  |
    | Pin 6 (GND)   | GND (Red wire)    |
    | GPIO 14 (TXD) | RX  (Green wire)  |
    | GPIO 15 (RXD) | TX (Yellow wire)  |

    <p float="left">
        <img src="img/rpi_conn.png" height="400">
        <img src="img/LCD_conn.png" height="400">
    </p>

### USB to UART Converter
Quite simple, just remember to cross RX and TX on the LCD and the USB/UART HW.
| USB <-> UART HW | LCD               |
| --------------- | ----------------- |
| 5V              | 5V  (Black wire)  |
| GND             | GND (Red wire)    |
| TXD             | RX  (Green wire)  |
| RXD             | TX (Yellow wire)  |

<p float="left">
    <img src="img/USB_conn.png" height="400">
    <img src="img/LCD_conn.png" height="400">
</p>

## Update the LCD screen firmware
1. Copy the LCD screen firmware `LCD/20240125.tft` to the root of a FAT32 formatted micro-SD card.
2. Make sure the LCD screen is powered off.
3. Insert the micro-SD card into the LCD screens SD card holder. Back-cover needs to be removed.
4. Power on the LCD screen and wait for screen to say `Update Successed!`

A more detailed guide on LCD screen firmware update can be found on the [Elegoo web-pages](https://www.elegoo.com/blogs/3d-printing/elegoo-neptune-3-pro-plus-max-fdm-3d-printer-support-files).


## Enable the UART
> **_Note_**: You can safely skip this section if you wired the display through a USB to UART converter
### [Disable Linux serial console](https://www.raspberrypi.org/documentation/configuration/uart.md)
  By default, the primary UART is assigned to the Linux console. If you wish to use the primary UART for other purposes, you must reconfigure Raspberry Pi OS. This can be done by using raspi-config:

  * Start raspi-config: `sudo raspi-config.`
  * Select option 3 - Interface Options.
  * Select option P6 - Serial Port.
  * At the prompt Would you like a login shell to be accessible over serial? answer 'No'
  * At the prompt Would you like the serial port hardware to be enabled? answer 'Yes'
  * Exit raspi-config and reboot the Pi for changes to take effect.
  
  For full instructions on how to use Device Tree overlays see [this page](https://www.raspberrypi.org/documentation/configuration/device-tree.md). 
  
  In brief, add a line to the `/boot/config.txt` file to apply a Device Tree overlay.
    
    dtoverlay=disable-bt

## Run the KlipperLCD service
SSH into your Raspberry Pi (or other SBC), then follow the steps below.

### 1. Verify Klipper socket API
Check that Klipper was started with a UNIX socket:

```bash
cat ~/printer_data/systemd/klipper.env
```

`KLIPPER_ARGS` must include:

```text
-a /home/<user>/printer_data/comms/klippy.sock
```

### 2. Install system dependencies
```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git rsync
```

### 3. Get the code
```bash
git clone https://github.com/joakimtoe/KlipperLCD
cd KlipperLCD
```

### 4. Configure environment
Service/runtime settings are read from:

```text
~/.config/KlipperLCD/service.env
```

Create/update this file from template:

```bash
make config
```

Key variables:
- `KLIPPERLCD_LCD_PORT` (for USB-UART typically `/dev/ttyUSB0`)
- `KLIPPERLCD_LCD_BAUDRATE`
- `KLIPPERLCD_KLIPPY_SOCK`
- `KLIPPERLCD_MOONRAKER_URL`
- `KLIPPERLCD_API_KEY`
- `KLIPPERLCD_UPDATE_INTERVAL`
- `KLIPPERLCD_LOG_LEVEL`

### 5. Install and enable systemd service
```bash
make install
```

This command:
- creates service venv (`~/KlipperLCD-venv` by default),
- installs package and dependencies,
- generates `/etc/systemd/system/KlipperLCD.service` from `service.template`,
- enables and starts the service.

### 6. Update / restart / remove
```bash
make upgrade      # reinstall package + restart service
make uninstall    # disable service + remove unit/venv
```

### 7. Development run (without systemd)
```bash
make venv
source .venv/bin/activate
python3 main.py
```

### Makefile commands
Show available commands any time:

```bash
make help
```

## Console
The console is enabled by default and can be accessed by clicking center top of the main screen or by clicking the thumbnail area while printing.

The console enables sending commands and will display all gcode responses and information from Klipper normally found in the console tab in Mainsail or Fluidd.

<p float="left">
    <img src="img/console.PNG" height="400">
    <img src="img/console_key.PNG" height="400">
    <img src="img/console_num.PNG" height="400">
</p>

## Thumbnails
KlipperLCD also supports thumbnails!

Follow this guide to enable thumbnails in your slicer: https://klipperscreen.readthedocs.io/en/latest/Thumbnails/

<p float="left">
    <img src="img/thumb1.png" height="400">
    <img src="img/thumb2.png" height="400">
</p>
