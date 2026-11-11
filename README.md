# Cell Counter

A desktop app that counts cells on Improved Neubauer hemocytometer images, from
saved photos or live from a microscope camera, and calculates the cell
concentration of a sample. It was developed for counting *Chlamydomonas* cells.

## What it does

1. **Straightens** the image so the grid lines are level.
2. **Finds the grid** and the 4×4 block of large squares (one 1 mm² corner of
   the chamber), and works out which corner it is.
3. **Finds cells**: small, dark, sharp-edged spots. Touching cells are split
   apart, and unusually faint cells are labelled "pale".
4. **Counts** cells in the block with the standard border rule: cells touching
   the top or left edge count, and cells touching the right or bottom edge don't.

Every detected cell is circled on screen. Click a circle to change it (cell →
pale → not a cell), or click empty space to add a missed cell. Images are
grouped into samples, one image per corner. The concentration is

> cells/mL = mean count per corner × 10⁴ × dilution factor

Results can be exported as CSV files.

In **live mode** the app counts the camera picture continuously (about 10 times
a second). Press Space to keep the current corner.

## Files

| File | What it is |
|---|---|
| `counter.py` | Counting engine (no user interface) |
| `app.py` | The desktop app |
| `stats_view.py` | Cell size window (histogram of cell areas) |
| `camera.py` | Connection to the microscope camera (RTSP video over Ethernet) |
| `first_pass.py` | Batch check of photos against hand counts in `Counts.txt` |
| `micromanager/` | Optional driver to use the camera in Micro-Manager |
| `environment.yml`, `requirements.txt` | Python libraries needed |
| `CellCounter.spec`, `build_windows.bat`, `BUILD_WINDOWS.md` | Building a stand-alone Windows program |

## Installing

You need Python 3.12.

With conda:

```
git clone https://github.com/maxgarwood/CellCounter.git
cd CellCounter
conda env create -f environment.yml
conda activate cellcounter
```

Or with pip:

```
git clone https://github.com/maxgarwood/CellCounter.git
cd CellCounter
python3.12 -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running

```
python app.py                    # open the app
python app.py photo1.jpg ...     # open the app with images loaded
python app.py --self-test        # check that everything is installed
python first_pass.py 1.jpg ...   # compare counts with Counts.txt
```

To build a stand-alone Windows program that doesn't need Python, see
[BUILD_WINDOWS.md](BUILD_WINDOWS.md).

### Live camera

The app was written for a KoPa DF12 microscope camera, which streams video
over its network cable at the fixed address 192.168.31.10. Plug the cable
into the computer and set the computer's wired network adapter to IP address
192.168.31.50, subnet mask 255.255.255.0, with no router (gateway). Then
click **Start live camera** in the app. Other cameras that stream RTSP
video can be used by changing the addresses at the top of `camera.py`.

## Assumptions

- An Improved Neubauer chamber: large squares of 250 µm, 0.1 mm deep.
- The grid square size in pixels (`SQUARE_PX` in `counter.py`) is set for one
  microscope magnification and camera. Change it if yours differs.

## Citation

If you use this software, please cite it. See [CITATION.cff](CITATION.cff), or
use "Cite this repository" on the GitHub page.

## Licence

MIT. See [LICENSE](LICENSE).
