import asyncio
import json
import logging
import os
import paho.mqtt.client as mqtt
from pathlib import Path

from idotmatrix.client import IDotMatrixClient
from idotmatrix.screensize import ScreenSize
from idotmatrix.util.image_utils import ResizeMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s :: %(levelname)s :: %(message)s")
logging.getLogger("bleak").setLevel(logging.WARNING)

import sys
import shutil

CONFIG_FILE = "config.json"
if not os.path.exists(CONFIG_FILE):
    logging.error(f"Configuration file {CONFIG_FILE} not found. Creating a template from config.example.json...")
    if os.path.exists("config.example.json"):
        shutil.copy("config.example.json", CONFIG_FILE)
    logging.error("Please fill out config.json and restart the server.")
    sys.exit(1)

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

MQTT_SERVER = config.get("mqtt_server", "")
MQTT_PORT = config.get("mqtt_port", 1883)
MQTT_USER = config.get("mqtt_user", "")
MQTT_PASS = config.get("mqtt_pass", "")
IDOTMATRIX_MAC = config.get("device_mac", "")
IMAGE_DIR = Path(config.get("image_dir", "images"))

if not MQTT_SERVER or not IDOTMATRIX_MAC or IDOTMATRIX_MAC == "00:00:00:00:00:00":
    logging.error("You must configure at least mqtt_server and device_mac in config.json")
    sys.exit(1)

# Global Queue for passing MQTT messages to Async Event Loop
msg_queue = asyncio.Queue()
EVENT_LOOP = None

def publish_discovery_messages(client):
    device_info = {
        "identifiers": ["idotmatrix_3f_4f"],
        "name": "iDotMatrix",
        "manufacturer": "iDotMatrix",
        "model": "LED Screen"
    }

    # Light
    light_config = {
        "schema": "json",
        "name": "iDotMatrix Display",
        "state_topic": "idotmatrix/display/light/state",
        "command_topic": "idotmatrix/display/light/command",
        "brightness": True,
        "supported_color_modes": ["rgb"],
        "optimistic": True,
        "qos": 0,
        "unique_id": "idotmatrix_light_module",
        "device": device_info
    }
    client.publish("homeassistant/light/idotmatrix_3f_4f/light/config", json.dumps(light_config), retain=True)

    # Text Input
    text_input_config = {
        "name": "iDotMatrix Input Text",
        "command_topic": "idotmatrix/display/text/command",
        "state_topic": "idotmatrix/display/text/state",
        "optimistic": True,
        "unique_id": "idotmatrix_text_input",
        "device": device_info
    }
    client.publish("homeassistant/text/idotmatrix_3f_4f/input_text/config", json.dumps(text_input_config), retain=True)

    # Image Loader
    image_loader_config = {
        "name": "iDotMatrix Image Loader",
        "command_topic": "idotmatrix/display/image/command",
        "state_topic": "idotmatrix/display/image/state",
        "optimistic": True,
        "unique_id": "idotmatrix_image_loader",
        "device": device_info
    }
    client.publish("homeassistant/text/idotmatrix_3f_4f/image_loader/config", json.dumps(image_loader_config), retain=True)

    # Mode Select
    mode_select_config = {
        "name": "iDotMatrix Mode",
        "command_topic": "idotmatrix/display/mode/command",
        "options": ["Clock", "Chronograph"],
        "optimistic": True,
        "unique_id": "idotmatrix_mode_select",
        "device": device_info
    }
    client.publish("homeassistant/select/idotmatrix_3f_4f/mode_select/config", json.dumps(mode_select_config), retain=True)

    # Effect Select
    effect_select_config = {
        "name": "iDotMatrix Effect",
        "command_topic": "idotmatrix/display/effect/command",
        "options": ["Effect 0", "Effect 1", "Effect 2", "Effect 3", "Effect 4", "Effect 5", "Effect 6"],
        "optimistic": True,
        "unique_id": "idotmatrix_effect_select",
        "device": device_info
    }
    client.publish("homeassistant/select/idotmatrix_3f_4f/effect_select/config", json.dumps(effect_select_config), retain=True)

    # Countdown Number
    countdown_number_config = {
        "name": "iDotMatrix Countdown",
        "command_topic": "idotmatrix/display/countdown/command",
        "min": 1,
        "max": 60,
        "step": 1,
        "optimistic": True,
        "unique_id": "idotmatrix_countdown_input",
        "device": device_info
    }
    client.publish("homeassistant/number/idotmatrix_3f_4f/countdown_input/config", json.dumps(countdown_number_config), retain=True)
    logging.info("Published MQTT Auto-Discovery components!")

# Ensure we're using MQTT v1 callback format compatible with paho-mqtt 2.1.0
def on_connect(client, userdata, flags, rc, properties=None):
    logging.info("Connected to MQTT broker")
    client.subscribe("idotmatrix/display/#")
    publish_discovery_messages(client)

def on_message(client, userdata, msg):
    try:
        if EVENT_LOOP is not None:
            EVENT_LOOP.call_soon_threadsafe(msg_queue.put_nowait, (msg.topic, msg.payload.decode('utf-8')))
        else:
            logging.error("Event loop is not initialized yet")
    except Exception as e:
        logging.error(f"Failed to queue message: {e}")

async def handle_commands(idotmatrix_client):
    while True:
        topic, payload = await msg_queue.get()
        logging.info(f"Received MQTT: {topic} -> {payload}")
        try:
            if topic == "idotmatrix/display/light/command":
                data = json.loads(payload)
                if "state" in data:
                    if data["state"] == "ON":
                        await idotmatrix_client.turn_on()
                    else:
                        await idotmatrix_client.turn_off()
                if "brightness" in data:
                    bright = max(5, int(data["brightness"] / 255.0 * 100))
                    await idotmatrix_client.set_brightness(bright)
                if "color" in data:
                    r = data["color"]["r"]
                    g = data["color"]["g"]
                    b = data["color"]["b"]
                    await idotmatrix_client.color.show_color((r, g, b))

            elif topic == "idotmatrix/display/text/command":
                if payload:
                    await idotmatrix_client.text.show_text(payload)

            elif topic == "idotmatrix/display/mode/command":
                if payload == "Clock":
                    await idotmatrix_client.clock.show()
                elif payload == "Chronograph":
                    await idotmatrix_client.chronograph.start_from_zero()

            elif topic == "idotmatrix/display/effect/command":
                if payload.startswith("Effect"):
                    effect_idx = int(payload.replace("Effect ", "").strip())
                    colours = ["red", "orange", "yellow", "green", "darkblue", "magenta", "white"]
                    await idotmatrix_client.effect.show(effect_idx, colours)

            elif topic == "idotmatrix/display/image/command":
                img_path = IMAGE_DIR / payload
                if img_path.exists() and img_path.is_file():
                    logging.info(f"Loading image from {img_path}")
                    if img_path.suffix.lower() == '.gif':
                        # Prepare device for GIF according to DigitalPictureFrame implementation
                        await idotmatrix_client.image.set_mode(0) # Disable DIY
                        await idotmatrix_client.color.show_color((0, 0, 0))
                        await idotmatrix_client.reset()
                        await idotmatrix_client.gif.upload_gif_file(img_path, ResizeMode.FILL)
                        await asyncio.sleep(3)
                    else:
                        # Prepare device for static Image according to DigitalPictureFrame implementation
                        await idotmatrix_client.image.set_mode(1) # Enable DIY
                        await idotmatrix_client.image.upload_image_file(img_path, ResizeMode.FILL)
                else:
                    logging.error(f"Image not found at {img_path}")

            elif topic == "idotmatrix/display/countdown/command":
                minutes = min(59, int(float(payload)))
                if minutes > 0:
                    await idotmatrix_client.countdown.start(minutes=minutes)
                    
        except Exception as e:
            logging.error(f"Error handling command {topic}: {e}")

async def main():
    global EVENT_LOOP
    EVENT_LOOP = asyncio.get_running_loop()

    if not IMAGE_DIR.exists():
        os.makedirs(IMAGE_DIR, exist_ok=True)
        
    logging.info("Initializing iDotMatrix Client...")
    idotmatrix_client = IDotMatrixClient(
        screen_size=ScreenSize.SIZE_32x32,
        mac_address=IDOTMATRIX_MAC
    )
    idotmatrix_client.set_auto_reconnect(True)
    
    try:
        await idotmatrix_client.connect()
        logging.info("Successfully connected to iDotMatrix!")
    except Exception as e:
        logging.error(f"Failed to connect initially, auto-reconnect should handle it: {e}")

    try:
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        mqtt_client = mqtt.Client()
        
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    if MQTT_USER and MQTT_PASS:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        
    mqtt_client.connect(MQTT_SERVER, MQTT_PORT, 60)
    mqtt_client.loop_start()

    logging.info("Awaiting MQTT commands...")
    await handle_commands(idotmatrix_client)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down...")
