# Musical Gcode Slicer

This Python slicer takes a monophonic MIDI file (`zelda-simple.mid`) and an STL (`triforce.stl`) and generates G-code that plays the MIDI file on loop while printing the model. It's currently hard-coded for PETG on the Ender 3 (240C/70C) or PLA on the Printrbot Plus V2.1 (200C/60C/fan off).

Instructions: run `musical_slicer.py` to generate `triforce_musical_ender3.gcode`, or `musical_slicer_ender3.py` to generate `triforce_musical_printrbot.py`.

Note: This only works with old noisy printers. Newer Ender 3s and other printers that use TMC2208/2209 and microstep interpolation make motor noise nearly silent. This works best on printers that use A4988s such as the initial run Ender 3s, Printrbot, or old marlin printers.

---

## Demo

Watch the demo on YouTube:

https://www.youtube.com/shorts/Ecfj6YbmK24