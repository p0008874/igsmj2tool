# igsmj2tool
IGSMJ_noelevate.bps Used for patching CD check (You need the original IGSMJ.EXE to patch this https://www.marcrobledo.com/RomPatcher.js/) (2001)

igsmj2_windowed.bps Used for patching CD check, CRC bypass, and Windowed (You need the original igsmj2.exe to patch this https://www.marcrobledo.com/RomPatcher.js/) (2002)

igsmj2_extractor.py Sprite and Sound extractor (2002)

keygen_new.py Well, the Keygen of course. (2002)


MJ2013/update.rom (For the 2013 version released in China.)
```
At 0x41896c: je 0x418ca0 — if update.rom [FLAG] name=gt365.com, it jumps PAST all serial validation straight to the clean exit. That's the hardcoded bypass string.
Place update.rom in the same directory as MJ2013.exe. That's it — no serial, no exe patch.
The logic at 0x41895b–0x41896c:
strcmp(update_rom[FLAG].name, "gt365.com")  →  je 0x418ca0  (clean exit, skips everything)
gt365.com is the hardcoded bypass value baked into the exe. When update.rom reports that name, all serial and login checks are jumped over entirely.
```
