# Making the Windows version of Cell Counter

A Windows program has to be built on a Windows computer. The script below does
everything; you only need to do this once (and again whenever the app changes).

## What you need

- A Windows 10 or 11 computer (64-bit).
- **Python 3.12** from <https://www.python.org/downloads/> (the "Windows installer (64-bit)").
  The lab tablet already has it. When installing, leave "py launcher" ticked.
- An internet connection the first time (it downloads about 150 MB of libraries).

## Build it

1. Copy the **"Windows build kit"** folder to the Windows computer (USB stick or OneDrive),
   e.g. to `Documents\Cell Counter build`. Unzip it first if it's a .zip.
2. Double-click **`build_windows.bat`**.
   - If Windows says "Windows protected your PC", click **More info → Run anyway**.
   - The first build takes about 5 minutes; later builds are quicker.
3. At the end, a **self-test** window lists checks. Everything should say **OK**.
   The camera line says FAIL unless the camera cable is plugged in; that's fine.
4. The app is now in `dist\Cell Counter\`. Double-click **`Cell Counter.exe`**.

To give it to labmates, send **`Cell Counter for Windows.zip`** (a few hundred MB).
They unzip it anywhere and double-click `Cell Counter.exe`; they don't need Python.
The first time, Windows may show "Windows protected your PC": **More info → Run anyway**.

## Where the app saves things

In **Documents\Cell Counter**: `captures` (kept live frames), `corrections`
(your click-corrections), exports, and `app_errors.log` if something goes wrong.

## Connecting the camera on Windows

Same as on the Mac: plug the camera's network cable into the computer (in place
of the lab display), then give the computer's network adapter a fixed address:

Settings → Network & internet → Ethernet → IP assignment → **Edit** → Manual → IPv4 on:
- IP address **192.168.31.50**, subnet prefix length **24** (or mask 255.255.255.0)
- Gateway and DNS left **empty**

If a Mac is connected to the camera at the same time (through a network switch),
use 192.168.31.51 on the Windows computer instead.

## If something goes wrong

- Run the check again: open the `dist\Cell Counter` folder, click the address bar, type
  `cmd`, press Enter, then type `"Cell Counter.exe" --self-test` and press Enter.
- The results are also saved in `Documents\Cell Counter\self_test.txt`; send that file
  (and `app_errors.log`, if there is one) along with a description of the problem.
