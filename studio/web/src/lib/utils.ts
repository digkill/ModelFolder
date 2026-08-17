import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const apiBase = `${import.meta.env.BASE_URL.replace(/\/$/, "")}/api/v1`;

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${apiBase}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json() as Promise<T>;
}

export function isMeshUrl(url?: string, ext?: unknown) {
  const fromMeta = String(ext || "").toLowerCase().replace(/^\./, "");
  if (fromMeta === "glb" || fromMeta === "gltf") return true;
  return /\.(glb|gltf)(\?|#|$)/i.test(url || "");
}
