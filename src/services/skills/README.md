# Skill sources and overrides

AgileCode bundles standard skills in the extension, so they are available without writing files to a workspace or user directory. Bundled skills are read-only and are identified by the `built-in` source.

Skills are resolved by identity (their `name`) for the active mode. Source precedence is **project > global > built-in**. Within one source, a mode-specific definition takes precedence over a generic definition. Consequently, a global or project skill with the same name replaces a bundled default; deleting that file reveals the bundled skill again. Project and global discovery paths and file watchers are unchanged.
