import os
import time
import gc
import random

import board
import storage
import sdcardio
import displayio

from digitalio import DigitalInOut

from adafruit_esp32spi import adafruit_esp32spi
import adafruit_requests
import adafruit_connection_manager


# ============================================================
# CONFIG
# ============================================================

SLIDE_SECONDS = 15
CHECK_FOR_NEW_IMAGES_SECONDS = 10 * 60

IMAGE_FOLDER = "/sd/images"
INDEX_FILE_NAME = "index.txt"
IMAGE_EXTENSION = ".bmp"

DOWNLOAD_CHUNK_SIZE = 512

# Set to True only if you want to redownload every file on every sync.
FORCE_DOWNLOAD = False


# ============================================================
# BASIC HELPERS
# ============================================================

print()
print("========================================")
print("PYPORTAL INDEX.TXT BMP SLIDESHOW")
print("RANDOMIZED, AUTO-CLEANUP, AUTO-SYNC")
print("========================================")
print()


try:
    random.seed(int(time.monotonic() * 1000))
except Exception as e:
    print("Random seed failed, continuing anyway.")
    print("ERROR:", repr(e))


def pause(message, seconds=2):
    print()
    print("----", message, "----")
    time.sleep(seconds)


def fail(step, error):
    print()
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    print("FAILED:", step)
    print("ERROR:", repr(error))
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    print()
    while True:
        time.sleep(1)


def getenv(name, default=None):
    value = os.getenv(name)
    if value is None:
        return default
    return value


def join_url(base, path):
    if base.endswith("/"):
        base = base[:-1]
    if path.startswith("/"):
        path = path[1:]
    return base + "/" + path


def ensure_folder(path):
    try:
        os.stat(path)
    except OSError:
        print("Creating folder:", path)
        os.mkdir(path)


def file_exists_and_has_size(path):
    try:
        return os.stat(path)[6] > 0
    except OSError:
        return False


def list_directory(path):
    print()
    print("Listing:", path)

    try:
        items = os.listdir(path)
        if not items:
            print(" - empty")
        else:
            for item in items:
                print(" -", item)
    except Exception as e:
        print("Could not list:", path)
        print("ERROR:", repr(e))


def validate_bmp(path):
    with open(path, "rb") as f:
        magic = f.read(2)

    if magic != b"BM":
        raise RuntimeError("Not a valid BMP file: " + path)


# ============================================================
# SHARED SPI
# ============================================================

pause("Creating shared SPI bus")

try:
    spi = board.SPI()
    print("SPI created.")
except Exception as e:
    fail("Create SPI", e)


# ============================================================
# SD CARD
# ============================================================

def mount_sd():
    pause("Mounting SD card")

    try:
        try:
            storage.umount("/sd")
            print("Unmounted previous /sd mount.")
        except Exception:
            print("No previous /sd mount.")

        sdcard = sdcardio.SDCard(spi, board.SD_CS)
        vfs = storage.VfsFat(sdcard)
        storage.mount(vfs, "/sd")

        print("Mounted SD at /sd.")
        list_directory("/sd")

    except Exception as e:
        fail("SD mount", e)


def write_local_sd_test():
    pause("Writing local SD test file")

    test_path = "/sd/slideshow_local_test.txt"

    try:
        with open(test_path, "w") as f:
            f.write("PyPortal SD write test worked.\n")

        with open(test_path, "r") as f:
            text = f.read()

        if "worked" not in text:
            raise RuntimeError("SD read-back did not match")

        print("Local SD write passed.")

    except Exception as e:
        fail("Local SD write", e)


# ============================================================
# ESP32 WIFI
# ============================================================

def create_esp32():
    pause("Creating ESP32 WiFi object")

    try:
        esp32_cs = DigitalInOut(board.ESP_CS)
        esp32_ready = DigitalInOut(board.ESP_BUSY)
        esp32_reset = DigitalInOut(board.ESP_RESET)

        esp = adafruit_esp32spi.ESP_SPIcontrol(
            spi,
            esp32_cs,
            esp32_ready,
            esp32_reset
        )

        print("ESP object created.")
        print("Firmware version:", esp.firmware_version)
        print("MAC address:", ":".join("{:02X}".format(b) for b in esp.MAC_address))

        return esp

    except Exception as e:
        fail("Create ESP32", e)


def connect_wifi(esp):
    pause("Connecting WiFi")

    networks = [
        (getenv("WIFI_SSID_1"), getenv("WIFI_PASSWORD_1")),
        (getenv("WIFI_SSID_2"), getenv("WIFI_PASSWORD_2")),
        (getenv("WIFI_SSID_3"), getenv("WIFI_PASSWORD_3")),
    ]

    for ssid, password in networks:
        if not ssid or not password:
            continue

        print("Trying WiFi:", ssid)

        try:
            esp.connect_AP(ssid, password)

            print("Connected to:", esp.ap_info.ssid)
            print("RSSI:", esp.ap_info.rssi)
            print("IP:", esp.ipv4_address)

            return

        except Exception as e:
            print("WiFi failed:", ssid)
            print("ERROR:", repr(e))
            time.sleep(3)
            gc.collect()

    raise RuntimeError("No WiFi networks connected")


def create_requests_session(esp):
    pause("Creating requests session")

    try:
        pool = adafruit_connection_manager.get_radio_socketpool(esp)
        ssl_context = adafruit_connection_manager.get_radio_ssl_context(esp)
        requests = adafruit_requests.Session(pool, ssl_context)

        print("Requests session created.")
        return requests

    except Exception as e:
        fail("Create requests session", e)


# ============================================================
# GITHUB INDEX.TXT
# ============================================================

def get_github_raw_base():
    base = getenv("GITHUB_RAW_BASE")

    if not base:
        raise RuntimeError("Missing GITHUB_RAW_BASE in settings.toml")

    if base.endswith("/"):
        base = base[:-1]

    print("GitHub raw base:")
    print(base)

    return base


def fetch_index_text(requests, raw_base):
    url = join_url(raw_base, INDEX_FILE_NAME)

    print()
    print("Fetching index:")
    print(url)

    response = None

    try:
        response = requests.get(
            url,
            headers={"User-Agent": "PyPortal-Slideshow"}
        )

        print("Index HTTP status:", response.status_code)

        if response.status_code != 200:
            raise RuntimeError("Index HTTP status " + str(response.status_code))

        text = response.text
        print("Index length:", len(text))

        return text

    finally:
        if response:
            response.close()
        gc.collect()


def parse_index_text(text):
    names = []

    for line in text.split("\n"):
        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        if line.lower().endswith(IMAGE_EXTENSION):
            names.append(line)

    print("Images listed in index.txt:")
    if not names:
        print(" - none")
    else:
        for name in names:
            print(" -", name)

    return names


def fetch_index_image_names(requests, raw_base):
    text = fetch_index_text(requests, raw_base)
    return parse_index_text(text)


# ============================================================
# LOCAL CLEANUP
# ============================================================

def local_name_from_index_name(index_name):
    return index_name.split("/")[-1]


def cleanup_removed_local_images(index_names):
    """
    Deletes local BMP files from /sd/images if they are no longer listed
    in index.txt.

    This runs on boot and on every sync check.
    """

    print()
    print("Checking for local BMP files removed from index.txt...")

    ensure_folder(IMAGE_FOLDER)

    valid_local_names_lower = {}

    for index_name in index_names:
        local_name = local_name_from_index_name(index_name)
        valid_local_names_lower[local_name.lower()] = True

    try:
        local_items = os.listdir(IMAGE_FOLDER)
    except Exception as e:
        print("Could not list image folder for cleanup.")
        print("ERROR:", repr(e))
        return

    for item in local_items:
        item_lower = item.lower()

        if not item_lower.endswith(IMAGE_EXTENSION):
            continue

        if item_lower in valid_local_names_lower:
            continue

        path = IMAGE_FOLDER + "/" + item

        print("Removing local image no longer in index.txt:", path)

        try:
            os.remove(path)
            print("Removed:", path)
        except Exception as e:
            print("Could not remove:", path)
            print("ERROR:", repr(e))

    print("Cleanup check complete.")


# ============================================================
# IMAGE DOWNLOAD
# ============================================================

def download_one_image(requests, raw_base, image_name):
    local_name = local_name_from_index_name(image_name)
    local_path = IMAGE_FOLDER + "/" + local_name
    url = join_url(raw_base, image_name)

    print()
    print("----------------------------------------")
    print("Image:", image_name)
    print("URL:", url)
    print("Local:", local_path)
    print("----------------------------------------")

    if not FORCE_DOWNLOAD and file_exists_and_has_size(local_path):
        try:
            validate_bmp(local_path)
            print("Already have valid BMP:", local_path)
            return local_path
        except Exception as e:
            print("Existing file is invalid. Redownloading.")
            print("ERROR:", repr(e))

    response = None

    try:
        response = requests.get(
            url,
            headers={"User-Agent": "PyPortal-Slideshow"}
        )

        print("Image HTTP status:", response.status_code)

        if response.status_code != 200:
            raise RuntimeError("Image HTTP status " + str(response.status_code))

        bytes_written = 0
        last_report = 0

        with open(local_path, "wb") as f:
            for chunk in response.iter_content(DOWNLOAD_CHUNK_SIZE):
                if not chunk:
                    continue

                f.write(chunk)
                bytes_written += len(chunk)

                if bytes_written - last_report >= 8192:
                    print("Downloaded bytes:", bytes_written)
                    last_report = bytes_written

        print("Download complete.")
        print("Bytes written:", bytes_written)

        if bytes_written <= 0:
            raise RuntimeError("Downloaded file was empty")

        validate_bmp(local_path)

        return local_path

    except Exception as e:
        print("Download failed for:", image_name)
        print("ERROR:", repr(e))

        try:
            os.remove(local_path)
            print("Removed partial file:", local_path)
        except Exception:
            pass

        raise

    finally:
        if response:
            response.close()

        gc.collect()
        time.sleep(1)


def sync_images_from_index(requests, raw_base, current_paths):
    print()
    print("========================================")
    print("SYNC CHECK START")
    print("========================================")

    ensure_folder(IMAGE_FOLDER)

    index_names = fetch_index_image_names(requests, raw_base)

    # This is the new cleanup behavior.
    # If an image was removed from index.txt, remove its local copy too.
    cleanup_removed_local_images(index_names)

    if not index_names:
        print("No images listed in index.txt.")
        print("Returning empty slideshow list.")
        return []

    updated_paths = []

    for image_name in index_names:
        local_name = local_name_from_index_name(image_name)
        local_path = IMAGE_FOLDER + "/" + local_name

        try:
            if FORCE_DOWNLOAD or not file_exists_and_has_size(local_path):
                print("New or missing image. Downloading:", image_name)
                local_path = download_one_image(requests, raw_base, image_name)
            else:
                validate_bmp(local_path)
                print("Already present:", local_path)

            updated_paths.append(local_path)

        except Exception as e:
            print("Could not use image:", image_name)
            print("ERROR:", repr(e))

    print()
    print("Updated slideshow list:")
    if not updated_paths:
        print(" - none")
    else:
        for path in updated_paths:
            print(" -", path)

    list_directory(IMAGE_FOLDER)

    print("========================================")
    print("SYNC CHECK COMPLETE")
    print("========================================")
    print()

    return updated_paths


# ============================================================
# RANDOMIZED SLIDESHOW
# ============================================================

def set_display_group(group):
    try:
        board.DISPLAY.root_group = group
    except AttributeError:
        board.DISPLAY.show(group)


def show_bmp(path):
    print("Showing:", path)

    bitmap = displayio.OnDiskBitmap(path)

    try:
        shader = bitmap.pixel_shader
    except AttributeError:
        shader = displayio.ColorConverter()

    tile_grid = displayio.TileGrid(bitmap, pixel_shader=shader)

    group = displayio.Group()
    group.append(tile_grid)

    set_display_group(group)


def make_random_deck(paths, last_path):
    deck = []

    for path in paths:
        deck.append(path)

    # Fisher-Yates shuffle.
    for i in range(len(deck) - 1, 0, -1):
        j = random.randrange(i + 1)
        temp = deck[i]
        deck[i] = deck[j]
        deck[j] = temp

    # Since we pop from the end, avoid showing the same image twice in a row
    # when possible.
    if len(deck) > 1 and deck[-1] == last_path:
        temp = deck[-1]
        deck[-1] = deck[0]
        deck[0] = temp

    print()
    print("Randomized slide deck:")
    for path in deck:
        print(" -", path)

    return deck


def run_slideshow(requests, raw_base, image_paths):
    pause("Starting slideshow", 2)

    deck = []
    last_path = None
    next_sync_time = time.monotonic() + CHECK_FOR_NEW_IMAGES_SECONDS

    while True:
        now = time.monotonic()

        # Check for changes between slide changes.
        # The currently displayed image stays on screen while sync runs.
        if now >= next_sync_time:
            try:
                print()
                print("Time to check index.txt for changes.")

                image_paths = sync_images_from_index(
                    requests,
                    raw_base,
                    image_paths
                )

                # Rebuild randomized deck after any sync.
                deck = []

            except Exception as e:
                print("Sync failed. Continuing slideshow with existing images.")
                print("ERROR:", repr(e))

            next_sync_time = time.monotonic() + CHECK_FOR_NEW_IMAGES_SECONDS

        if not image_paths:
            print("No images available for slideshow. Waiting...")
            time.sleep(SLIDE_SECONDS)
            continue

        if not deck:
            deck = make_random_deck(image_paths, last_path)

        path = deck.pop()

        try:
            show_bmp(path)
            last_path = path
        except Exception as e:
            print("Display failed for:", path)
            print("ERROR:", repr(e))

        gc.collect()
        time.sleep(SLIDE_SECONDS)


# ============================================================
# MAIN
# ============================================================

try:
    mount_sd()
    write_local_sd_test()
    ensure_folder(IMAGE_FOLDER)

    esp = create_esp32()
    connect_wifi(esp)

    requests = create_requests_session(esp)
    raw_base = get_github_raw_base()

    # Initial boot sync:
    # 1. Reads index.txt
    # 2. Deletes local BMPs no longer listed
    # 3. Downloads missing BMPs
    image_paths = sync_images_from_index(
        requests,
        raw_base,
        []
    )

    print()
    print("========================================")
    print("INITIAL SYNC COMPLETE")
    print("Starting randomized slideshow.")
    print("Each image displays for 15 seconds.")
    print("Will check index.txt every 10 minutes.")
    print("========================================")
    print()

    run_slideshow(requests, raw_base, image_paths)

except Exception as e:
    fail("Main program", e)