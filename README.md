# Image to PDF Converter

A Windows desktop tool that converts scanned images to PDF in bulk. You pick a folder and it goes through every subfolder. Depending on the mode, each folder becomes one PDF, or each image becomes its own PDF. The PDFs are saved next to the original images.

It was built for the Indexing team to convert large scanned archives (hundreds of images across many subfolders) in one click. It is a single `.exe`, so it needs no installation and no Python.

## Features

- **Two output modes**
  - **One PDF per folder** (default): all images in a folder are combined into `<folder name>.pdf` and saved inside that folder. Each subfolder gets its own PDF.
  - **One PDF per image**: `scan001.jpg` becomes `scan001.pdf` in the same folder.
- **Pages in the correct order.** Files are sorted by number, so `page2` comes before `page10`.
- **No quality loss for JPEGs.** They go into the PDF byte-for-byte as they are, without re-compression. Other formats are stored without losing any detail.
- **Formats:** JPG, PNG, BMP, GIF, TIFF (multi-page TIFFs become multi-page PDFs) and WebP. Transparent images are placed on a white background.
- **Safe to re-run.** Folders or images that already have a PDF are skipped by default, so running it again only converts what is new.
- **Bad files don't stop the batch.** Corrupt or unreadable images are reported in red in the log and left out, and the rest still convert.
- **Live progress:** an animated percentage and progress bar, elapsed time, estimated time left, and images per second.
- **Fast.** Images are converted in parallel on up to 8 CPU cores. On a 12-core PC, 340 A4 scans at 300 DPI (300 JPEGs and 40 colour TIFFs) took about 10 seconds.
- **No half-written files.** If you press **Stop** or the app is closed during a run, no broken PDF is left behind.

## Using it

1. Run `ImageToPDF.exe`, or drag a folder onto the exe icon.
2. Click **Browse...** and choose the folder. The app shows how many images it found and how many PDFs it will create.
3. Choose the output mode and click **Convert to PDF**.
4. When it finishes, check the log for any images shown in red as **LEFT OUT** or **FAILED**.

| Option | Default | What it does |
|---|---|---|
| Include subfolders | On | Also converts images in all subfolders |
| Skip if the PDF already exists | On | Leaves existing PDFs untouched. Turn it off to rebuild them. |

> Windows SmartScreen or antivirus software may warn about the exe because it is not code-signed. Choose **More info → Run anyway**, or ask IT to whitelist it.

## Building from source

Requires Windows and Python 3.10 or newer (developed on 3.13).

```bat
build.bat
```

The script creates a `.venv`, installs the dependencies and writes the exe to `dist\ImageToPDF.exe`.

To run it without building:

```bat
python -m venv .venv
.venv\Scripts\pip install pillow img2pdf pikepdf
.venv\Scripts\python image_to_pdf.py
```

## How it works

- **[img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf)** creates each page. It puts JPEG data into the PDF unchanged and keeps TIFF and PNG lossless. Formats it can't handle (WebP, some colour modes) are first converted with **Pillow**.
- **[pikepdf](https://github.com/pikepdf/pikepdf)** joins the pages into one PDF per folder by copying them, so the image data is not touched.
- The interface is **Tkinter**, packaged into a single exe with **PyInstaller**.

## Limitations

- In folder mode, all images of one folder are held in memory while that folder's PDF is built. Up to two folders are built at a time.
- For folders that are only JPEGs, the speed of the disk sets the limit. Working on a local SSD is much faster than working on a network drive.
- The time estimate can be low at the start when fast and slow files are mixed (for example JPEGs followed by large TIFFs). It corrects itself as the run goes on.

## Project structure

```
image_to_pdf.py   The app: conversion logic and GUI
build.bat         Builds dist\ImageToPDF.exe
```

## Author

**Muhammad Babar Saleem**
Phone: +92 315 6500221

## License

Released into the public domain under [The Unlicense](LICENSE).
