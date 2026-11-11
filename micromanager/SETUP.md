# KoPa camera in Micro-Manager: setup on Windows

This lets Micro-Manager use the KoPa DF12 microscope camera over its network cable. It uses Micro-Manager's **PyDevice** adapter and one Python file, `kopa_camera.py`.

**You need:**
- Windows with **Python 3.9 or newer** (3.12 recommended)
- **Micro-Manager 2.0.3** with PyDevice
- The camera's network cable plugged into this computer, with the network adapter set to **IP 192.168.31.50**, **subnet prefix length 24** (subnet mask 255.255.255.0), and **no gateway**

---

## 1. Put the driver in its own folder

1. Create a folder, for example `Documents\KoPaCamera`.
2. Copy **`kopa_camera.py`** into it (by USB stick, OneDrive or email).

## 2. Give it its own Python environment (once)

PyDevice automatically uses a Python environment called `venv` if it finds one next to the script. This keeps the driver's packages separate from anything else on the computer.

Open **Command Prompt** and run these one at a time (wait for each to finish):

```
cd %USERPROFILE%\Documents\KoPaCamera
python -m venv venv
venv\Scripts\python -m pip install numpy opencv-python annotated-types
```

The last command should end with "Successfully installed…".

## 3. Test it before opening Micro-Manager

With the camera cable plugged in:

```
venv\Scripts\python kopa_camera.py
```

You should see something like:

```
Connecting to rtsp://192.168.31.10:554/av0_1 ...
OK: got a 1600 x 1200 image, uint16, brightness 514-56797
Frame rate: about 29 pictures per second
Saved a test picture to ...\KoPaCamera\kopa_test.png
```

Open `kopa_test.png` to check it shows the microscope view. **If this step fails, Micro-Manager won't work either.** Send the error message to get help.

## 4. Add the camera in Micro-Manager

1. Start Micro-Manager. If it asks for a configuration, choose **(none)**.
2. **Devices → Hardware Configuration Wizard → Create new configuration → Next.**
3. In the list of available devices, expand **PyDevice**, select **PyHub**, and click **Add**.
4. A settings window opens:
   - **ScriptPath:** click to browse to `Documents\KoPaCamera\kopa_camera.py`.
   - **PythonEnvironment:** leave it as **(auto)**. It finds the `venv` folder you made.
   - Click **OK**.
5. It should now offer a camera called **KoPa**. Tick it or click **Add**, then **OK**.
6. Click **Next** through the remaining pages. On the default devices page, check that **Camera = KoPa**.
7. Save the configuration, for example as `KoPa.cfg`. Next time, choose this configuration when Micro-Manager starts.

## 5. Using it

- **Live** and **Snap** work as normal. Each snap is a fresh picture taken after you click, never an old one.
- The image is **black-and-white, 16-bit**. Micro-Manager's PyDevice doesn't support colour. In **Devices → Device Property Browser**, the **KoPa-Channel** setting chooses **Grey** (normal), or just the **Red**, **Green** or **Blue** part of the colour picture.
- **The region of interest (ROI)** tools work.
- **"Exposure" has no effect.** Exposure, gain and white balance are controlled by the camera itself, in its own display software. The setting only exists because Micro-Manager requires every camera to have one.
- **Read-only info** in the Device Property Browser:
  - **KoPa-Connected** shows whether pictures are arriving.
  - **KoPa-Stream** shows which video stream is used.

## 6. Full resolution (optional)

By default the driver uses the camera's **1600 × 1200** stream, which suits a tablet. For the full **4000 × 3000**:
1. Open `kopa_camera.py` in Notepad.
2. Change `STREAM = "small"` to `STREAM = "full"`.
3. Save, then **restart Micro-Manager**.

Full resolution is about 6× more data per picture, so Live mode may be slow on a tablet.

## Troubleshooting

| Problem | What to check |
|---|---|
| Step 3 says "No picture from the camera…" | The cable is plugged in; the adapter IP is 192.168.31.50, prefix 24, no gateway; `ping 192.168.31.10` replies; VLC can open `rtsp://192.168.31.10:554/av0_1` |
| Step 3 says "No module named …" | Rerun the `pip install` line from step 2 |
| PyHub doesn't list a **KoPa** camera | Use step 3 to confirm the script runs. Make sure the `venv` folder is in the same folder as `kopa_camera.py`. |
| Live view freezes, then Micro-Manager shows an error | The connection dropped. The driver reconnects by itself, so wait a few seconds and press Live again. |
| The lab display shows nothing | The camera has one network socket. Unplug this computer and plug the display back in, or use a small network switch so both can stay connected. |

**Note:** PyDevice is marked *experimental* by the Micro-Manager team, and future Micro-Manager updates may change how it works. This driver was written for and checked against PyDevice as of November 2026.
