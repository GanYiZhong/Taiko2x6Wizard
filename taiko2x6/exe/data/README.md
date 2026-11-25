# Bundled data

`KBD2EE.irx` — the IOP module of the Taiko 14 USB keyboard (+ dummy JVS)
patch, "full" build, sha256 `34f1cc379fb27157c2fac4f628fece3257b1ac5fc820005716cb67733bf4fe17`.
It is the exact binary validated on hardware, copied from `prebuilt/` of
[Taiko14-USB-Keyboard-Patch](https://github.com/GanYiZhong/Taiko14-USB-Keyboard-Patch)
(MIT). Source, build steps and reverse-engineering notes live in that
repository. `taiko2x6/exe/usb_keyboard.py` refuses any other binary.

`KBD2EE_kbdonly.irx` — the same module built with `gen_blob.py --kbdonly`
(keyboard only, for a machine that keeps its real I/O board; none of the
dummy-JVS hooks), sha256
`719c4379ef7e31ca74d6284e44740b59695d3f1c74dded36db156b493b658b87`. Built with
the ps2dev toolchain that reproduces the full build above bit for bit, so the
two differ only in the hook table. Not yet tested on hardware.
