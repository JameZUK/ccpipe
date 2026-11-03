// What kind of file a name is, for the file panel's icons and the viewer.
// The media list matches the server's inline allowlist
// (backend routes/fs_transfer.py INLINE_TYPES): anything else is shown as
// text if it reads as UTF-8, or offered for download.

import {
  ICON_FILE, ICON_FILE_AUDIO, ICON_FILE_CODE, ICON_FILE_IMAGE, ICON_FILE_PDF, ICON_FILE_VIDEO,
} from "./icons";

export type FileKind = "image" | "pdf" | "audio" | "video" | "markdown" | "text";

const BY_EXT: Record<string, FileKind> = {};
const add = (kind: FileKind, exts: string): void => {
  for (const e of exts.split(" ")) BY_EXT[e] = kind;
};
add("image", "png jpg jpeg gif webp avif bmp ico svg");
add("pdf", "pdf");
add("audio", "mp3 m4a aac wav flac ogg oga opus weba");
add("video", "mp4 m4v webm ogv mov mkv");
add("markdown", "md markdown");

export function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i + 1).toLowerCase() : "";
}

export function fileKind(name: string): FileKind {
  return BY_EXT[extOf(name)] ?? "text";
}

/** Where the viewer gets an image, PDF, audio or video file. */
export function inlineUrl(path: string): string {
  return `/api/fs/inline?path=${encodeURIComponent(path)}`;
}

// Extensions shown with the code icon in the list (a hint only: every
// non-media file opens as text).
const CODE_EXTS = new Set(
  ("js mjs cjs ts tsx jsx py rb go rs java kt swift c h cc cpp hpp cs php sh bash zsh fish "
    + "lua pl r sql json yaml yml toml ini cfg conf xml html htm css scss vue svelte "
    + "dockerfile makefile mk gradle tf nix el clj ex exs erl hs ml scala dart zig").split(" "),
);

export function kindIcon(name: string): string {
  switch (fileKind(name)) {
    case "image": return ICON_FILE_IMAGE;
    case "pdf": return ICON_FILE_PDF;
    case "audio": return ICON_FILE_AUDIO;
    case "video": return ICON_FILE_VIDEO;
    default: return CODE_EXTS.has(extOf(name)) || CODE_EXTS.has(name.toLowerCase()) ? ICON_FILE_CODE : ICON_FILE;
  }
}

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
