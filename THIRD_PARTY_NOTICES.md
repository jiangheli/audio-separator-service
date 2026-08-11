# Third-party notices

StemFlow uses and redistributes components from the open-source audio separation
ecosystem. Their original authors retain all rights to their respective work.

## python-audio-separator

- Project: https://github.com/nomadkaraoke/python-audio-separator
- License: MIT
- Author: Andrew Beveridge and contributors

`python-audio-separator` provides the public Python adapter used by StemFlow and
includes code derived from Ultimate Vocal Remover GUI.

## Ultimate Vocal Remover and MDX-Net

- Ultimate Vocal Remover GUI: https://github.com/Anjok07/ultimatevocalremovergui
- Model repository: https://github.com/TRvlvr/model_repo
- Default bundled model: `UVR-MDX-NET-Inst_HQ_3.onnx`

Credit belongs to the UVR developers and model authors, including Anjok07,
DilanBoskan, Kuielab, Woosung Choi, KimberleyJSN, Hv, and the wider UVR
community. StemFlow does not modify the model inference algorithm.

## Other runtime components

The installer also contains Python, PyTorch, ONNX Runtime, FFmpeg,
imageio-ffmpeg, NumPy, SciPy, librosa, and their transitive dependencies.
License files shipped inside their distributions remain applicable.
