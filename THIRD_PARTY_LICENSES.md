# Third-party licences

ImageSL is licensed under Apache-2.0. It depends on, and its desktop builds redistribute,
the following. All are permissive; none imposes copyleft obligations on this
project or on binaries built from it.

## Runtime (bundled into the desktop application)

| Package | Licence | Role |
| --- | --- | --- |
| numpy | BSD-3-Clause | array maths |
| scipy | BSD-3-Clause | image filters, morphology |
| scikit-image | BSD-3-Clause | segmentation, colour deconvolution |
| Pillow | MIT-CMU (HPND) | image decoding |
| tifffile | BSD-3-Clause | TIFF reading and writing |
| imagecodecs | BSD-3-Clause | compressed and pyramidal TIFF codecs |
| fastapi | MIT | HTTP API |
| starlette | BSD-3-Clause | ASGI framework under FastAPI |
| pydantic | MIT | request validation |
| uvicorn | BSD-3-Clause | ASGI server |
| python-multipart | Apache-2.0 | multipart upload parsing |
| pywebview | BSD-3-Clause | native window shell |

## Build-time (not redistributed as source)

| Package | Licence | Note |
| --- | --- | --- |
| PyInstaller | GPL-2.0 **with bootloader exception** | see below |
| Inno Setup | Modified BSD | Windows installer generator |

### The PyInstaller exception matters

PyInstaller is GPL-2.0, but carries an explicit exception permitting the
bundling of applications under **any** licence, including proprietary ones.
Only the bootloader is linked into the produced executable, and the exception
covers exactly that. Freezing ImageSL with PyInstaller therefore does not make
the resulting binary GPL, and does not require ImageSL to be GPL.

Apache-2.0 is compatible with every dependency licence listed above.

If the bootloader is ever modified, the exception no longer applies to it and
the modified bootloader must be released under GPL-2.0. ImageSL does not
modify it.

## Reproducing this list

```
pip install pip-licenses
pip-licenses --from=mixed --format=markdown --with-urls
```

Run it against the environment in `desktop/requirements.txt` for the full
transitive set, which is larger than the direct dependencies listed above.
