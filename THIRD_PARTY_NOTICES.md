# Third-party notices

Portions of the video-observation and transcription workflow are provided under the MIT license.

Copyright (c) 2026 Bradley Bonanno

The native subtitle draft layout is adapted from [OpenFilm v0.4.1](https://github.com/openfilm/openfilm/blob/v0.4.1/packages/openfilm/studio/server/jianying.mjs) under the same MIT terms.

Copyright (c) 2026 OpenFilm contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The native encrypted-draft helper is adapted from `jy-draftc` under the MIT License.
Copyright (c) 2026 wenshui330. The complete license is included at
`native/LICENSE.jy-draftc`.

The Windows x64 distribution includes the helper compiled from `native/jy_draft_crypto.cpp` and a copy of its license beside the executable. The build used MinGW-w64 with `g++ -std=c++17 -O2 -s -static -municode -Wl,--no-insert-timestamp native/jy_draft_crypto.cpp -o jy-draftc.exe`. The distributed executable's SHA-256 is `94fe05995727a494a193c6d0711399495702fd9f40107f9d87a3af0ab3c41e0c`.

The installer retrieves Noto Sans SC and Noto Serif SC at Google Fonts revision `83c62e30d87daa70216dab6b984d00d20d96d585` and checks the SHA-256 of each font and license. Portable projects carry the selected font and its SIL Open Font License. [Noto Sans SC](https://github.com/google/fonts/tree/83c62e30d87daa70216dab6b984d00d20d96d585/ofl/notosanssc) · [Noto Serif SC](https://github.com/google/fonts/tree/83c62e30d87daa70216dab6b984d00d20d96d585/ofl/notoserifsc)
