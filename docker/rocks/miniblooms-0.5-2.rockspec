-- Builds the `minibloom` C module from an already-unpacked miniblooms source tree
-- (`luarocks make` from inside it); the Dockerfile fetches and checksums the source.
package = "miniblooms"
version = "0.5-2"
source = {
   url = "https://github.com/bitdivine/miniblooms",
}
description = {
   summary = "Small, fast bloom filters",
   homepage = "https://github.com/bitdivine/miniblooms",
   license = "MIT",
}
dependencies = {
   "lua >= 5.1, < 5.2",
}
build = {
   type = "builtin",
   modules = {
      minibloom = {
         sources = { "src/minibloom.c", "src/minibloomfile.c", "src/murmur.c", "minibloom.c" },
         incdirs = { "src/" },
      },
   },
}
