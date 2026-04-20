# iDotMatrix MQTT Bridge 

A standalone Python-based MQTT Bridge that seamlessly connects generic iDotMatrix LED displays directly to Home Assistant. By utilizing Home Assistant's native MQTT Auto-Discovery protocol, zero YAML configuration is needed.

This project packages the core functionality reverse-engineered by [markusressel/idotmatrix-api-client](https://github.com/markusressel/idotmatrix-api-client). The original python library acts as the engine driving the Bluetooth LE communications.

## Key Features
- **Auto-Discovery:** Devices immediately populate in Home Assistant across the network.
- **Local Control:** Bypasses cloud requirements, utilizing direct Bluetooth protocol exclusively.
- **Feature Complete:** Supports Power state, Brightness, RGB Color, Text Rendering, Animated GIFs, Static Images, Clock mechanisms, Chronographs, and built-in Visual Effects.

## Installation

This bridge should be deployed on a host computing environment possessing direct access to a physical Bluetooth adapter. 

1. **Clone this repository** to your host machine.
2. Verify Python 3.11+ is installed.
3. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Rename the `config.example.json` file to `config.json` and input your MQTT broker credentials and the iDotMatrix display's Bluetooth MAC address.
5. Execute the startup script:
   - For Windows users: Run `start_windows.bat`
   - Alternatively: Run `python idotmatrix_server.py`

Once executed, the script will instantly interface with your MQTT broker and provision all endpoints inside Home Assistant automatically.

## Uploading Images & GIFs
To render graphics from Home Assistant, place 32x32 `.png` or `.gif` files into the local `images/` directory. Several demo images are included by default. 

To display them, navigate to the `iDotMatrix` device page inside Home Assistant and type the respective filename (e.g., `demo.gif`) into the "Image Loader" text input entity.
