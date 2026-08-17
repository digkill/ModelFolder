import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Maximize2, X } from "lucide-react";
import { api, apiBase, isMeshUrl } from "@/lib/utils";
import { Badge, Card, Input } from "@/components/ui/primitives";
import { Button } from "@/components/ui/button";
import { Viewport } from "@/components/Viewport";

type Job = {
  id: string;
  agent: string;
  model: string;
  kind: string;
  status: string;
  error?: string | null;
  output?: Record<string, unknown>;
};
type Asset = { id: string; kind: string; title: string; url: string; meta?: Record<string, unknown>; created_at?: string };
type Message = { id: string; role: string; content: string };
type Project = {
  id: string;
  title: string;
  prompt: string;
  platform: string;
  status: string;
  plan: Record<string, unknown>;
  jobs: Job[];
  assets: Asset[];
  messages: Message[];
};

export default function ProjectPage() {
  const { id } = useParams();
  const [p, setP] = useState<Project | null>(null);
  const [engine, setEngine] = useState<"three" | "babylon">("three");
  const [chat, setChat] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"game" | "scene">("game");
  const [playMode, setPlayMode] = useState(false);
  const chatLog = useRef<HTMLDivElement>(null);
  const gameFrame = useRef<HTMLIFrameElement>(null);

  async function reload() {
    if (!id) return;
    const data = await api<Project>(`/projects/${id}`);
    setP(data);
  }

  useEffect(() => {
    reload().catch(console.error);
  }, [id]);

  useEffect(() => {
    if (!id) return;
    const es = new EventSource(`${apiBase}/projects/${id}/events`);
    es.onmessage = () => {
      reload().catch(() => undefined);
    };
    return () => es.close();
  }, [id]);

  const gameUrl = useMemo(() => {
    const games = (p?.assets || []).filter((a) => a.kind === "game" && a.url);
    const g = games.at(-1);
    if (!g?.url) return "";
    const stamp = encodeURIComponent(g.created_at || String(Date.now()));
    return `${g.url}${g.url.includes("?") ? "&" : "?"}v=${stamp}`;
  }, [p]);

  const modelUrl = useMemo(() => {
    const assets = p?.assets || [];
    const mesh = assets.find((a) => a.kind === "mesh" && isMeshUrl(a.url, a.meta?.ext));
    const model = assets.find((a) => a.kind === "model" && isMeshUrl(a.url, a.meta?.ext));
    return mesh?.url || model?.url;
  }, [p]);

  const splashUrl = useMemo(() => {
    const items = (p?.assets || []).filter((a) => a.kind === "image" && a.url);
    return items.at(-1)?.url || "";
  }, [p]);

  const previewUrl = useMemo(() => {
    const items = (p?.assets || []).filter(
      (a) => a.kind === "video" && a.url && !/\.(png|jpe?g|webp|gif)(\?|$)/i.test(a.url),
    );
    return items.at(-1)?.url || "";
  }, [p]);

  useEffect(() => {
    if (gameUrl) setTab("game");
  }, [gameUrl]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPlayMode(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const el = chatLog.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [p?.messages]);

  async function send() {
    if (!id || !chat.trim() || busy) return;
    const text = chat.trim();
    setBusy(true);
    setError("");
    try {
      await api(`/projects/${id}/chat`, { method: "POST", body: JSON.stringify({ message: text }) });
      setChat("");
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const gdd = (p?.plan?.gdd || null) as Record<string, unknown> | null;
  const reviews = (p?.plan?.reviews || null) as {
    playtest?: Record<string, unknown>;
    spec?: Record<string, unknown>;
  } | null;

  async function runReview() {
    if (!id || reviewing) return;
    setReviewing(true);
    setError("");
    try {
      await api(`/projects/${id}/review`, { method: "POST", body: "{}" });
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setReviewing(false);
    }
  }

  async function nativeFullscreen() {
    const el = gameFrame.current;
    if (!el) return;
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await el.requestFullscreen();
    } catch {
      /* ignore */
    }
  }

  if (!p) return <div className="p-10 text-zinc-500">Загрузка…</div>;

  return (
    <>
      {playMode && gameUrl && (
        <div className="fixed inset-0 z-50 bg-black">
          <iframe
            ref={gameFrame}
            title="mini-game"
            src={gameUrl}
            className="h-full w-full border-0 bg-black"
            allow="autoplay; fullscreen"
          />
          <div className="absolute right-4 top-4 z-10 flex gap-2">
            <Button size="sm" variant="ghost" onClick={() => void nativeFullscreen()}>
              <Maximize2 className="h-4 w-4" />
              Полный экран
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setPlayMode(false)}>
              <X className="h-4 w-4" />
              Студия
            </Button>
          </div>
        </div>
      )}
    <main className="mx-auto grid max-w-[1400px] gap-4 px-4 py-6 lg:grid-cols-[320px_1fr_340px]">
      <Card className="flex max-h-[78vh] flex-col p-4">
        <div className="text-xs text-zinc-500">Пайплайн агентов</div>
        <h2 className="mt-1 text-lg font-semibold">{p.title}</h2>
        <Badge className="mt-2 w-fit">{p.status}</Badge>
        <Button className="mt-3" size="sm" disabled={reviewing} onClick={() => void runReview()}>
          {reviewing ? "Ревью…" : "Ревью и плейтест"}
        </Button>
        {gdd && (
          <div className="mt-3 rounded-lg border border-violet-500/30 bg-violet-500/5 p-3 text-[11px] text-zinc-300">
            <div className="text-[10px] uppercase text-violet-300">ТЗ геймдизайнера</div>
            <div className="mt-1 font-medium">{String(gdd.title || p.title)}</div>
            <p className="mt-1 text-zinc-400">{String(gdd.core_loop || gdd.fantasy || "")}</p>
            {Array.isArray(gdd.must_have) && (
              <ul className="mt-2 list-disc space-y-0.5 pl-4 text-zinc-500">
                {(gdd.must_have as unknown[]).slice(0, 6).map((item, i) => (
                  <li key={i}>{String(item)}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        {reviews?.spec && (
          <div className="mt-3 rounded-lg border border-zinc-800 p-3 text-[11px]">
            <div className="text-[10px] uppercase text-zinc-500">Сверка с ТЗ</div>
            <div className="mt-1 text-zinc-200">
              {String(reviews.spec.match_percent ?? "—")}% · {String(reviews.spec.verdict || "")}
            </div>
            <p className="mt-1 text-zinc-500">{String(reviews.spec.summary || "")}</p>
          </div>
        )}
        {reviews?.playtest && (
          <div className="mt-3 rounded-lg border border-zinc-800 p-3 text-[11px]">
            <div className="text-[10px] uppercase text-zinc-500">Плейтест</div>
            <div className="mt-1 text-zinc-200">
              {String(reviews.playtest.score ?? "—")}/10 · {String(reviews.playtest.verdict || "")}
            </div>
            <p className="mt-1 text-zinc-500">{String(reviews.playtest.session_notes || "")}</p>
          </div>
        )}
        <div className="mt-4 space-y-2 overflow-auto">
          {(p.jobs || []).map((j) => (
            <div key={j.id} className="rounded-lg border border-zinc-800 p-3">
              <div className="flex items-center justify-between text-sm">
                <span>{j.agent}</span>
                <span className={j.status === "error" ? "text-red-400" : "text-zinc-400"}>{j.status}</span>
              </div>
              <div className="text-[11px] text-zinc-600">
                {j.kind} · {j.model}
              </div>
              {j.error && <div className="mt-1 text-[11px] text-red-400">{j.error}</div>}
            </div>
          ))}
        </div>
      </Card>

      <section className="space-y-4">
        <Card className="h-[52vh] overflow-hidden p-2">
          <div className="mb-2 flex items-center justify-between px-2 pt-1">
            <div className="flex gap-2">
              <Button size="sm" variant={tab === "game" ? "default" : "ghost"} onClick={() => setTab("game")}>
                Игра
              </Button>
              <Button size="sm" variant={tab === "scene" ? "default" : "ghost"} onClick={() => setTab("scene")}>
                Сцена
              </Button>
            </div>
            {tab === "scene" && (
              <div className="flex gap-2">
                <Button size="sm" variant={engine === "three" ? "default" : "ghost"} onClick={() => setEngine("three")}>
                  Three.js
                </Button>
                <Button size="sm" variant={engine === "babylon" ? "default" : "ghost"} onClick={() => setEngine("babylon")}>
                  Babylon.js
                </Button>
              </div>
            )}
            {tab === "game" && gameUrl && (
              <Button size="sm" onClick={() => setPlayMode(true)}>
                <Maximize2 className="h-4 w-4" />
                На весь экран
              </Button>
            )}
          </div>
          <div className="h-[calc(52vh-48px)]">
            {tab === "game" && gameUrl ? (
              <iframe title="mini-game" src={gameUrl} className="h-full w-full rounded-xl border-0 bg-black" allow="autoplay" />
            ) : tab === "game" && (previewUrl || splashUrl) ? (
              <div className="relative h-full overflow-hidden rounded-xl bg-black">
                {previewUrl ? (
                  <video
                    src={previewUrl}
                    poster={splashUrl || undefined}
                    autoPlay
                    muted
                    loop
                    playsInline
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <img src={splashUrl} alt="" className="h-full w-full object-cover" />
                )}
                <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 to-transparent p-4 text-sm text-zinc-200">
                  <div className="text-[10px] uppercase tracking-wide text-zinc-400">
                    {previewUrl ? "Превью" : "Сплеш"}
                  </div>
                  <div className="mt-1">{p.title}</div>
                  <p className="mt-1 text-xs text-zinc-400">
                    Мини-игры ещё нет. Напиши в чат «реализуй мини-игру» — сплеш и ролик уже подхватятся.
                  </p>
                </div>
              </div>
            ) : tab === "game" ? (
              <div className="flex h-full items-center justify-center px-6 text-center text-sm text-zinc-500">
                Мини-игры ещё нет. Напиши в чат «реализуй мини-игру» — оркестратор соберёт прототип из моделей каталога.
              </div>
            ) : (
              <Viewport url={modelUrl} engine={engine} />
            )}
          </div>
        </Card>
        <div className="grid gap-3 md:grid-cols-2">
          {(p.assets || [])
            .filter((a) => a.kind === "image" || a.kind === "video" || a.kind === "audio" || a.kind === "music")
            .map((a) => (
              <Card key={a.id} className="overflow-hidden p-3">
                <div className="mb-2 text-xs text-zinc-500">
                  {a.kind === "image" ? "Сплеш" : a.kind === "video" ? "Превью" : a.kind}
                </div>
                {a.kind === "image" && a.url && <img src={a.url} alt={a.title} className="w-full rounded-lg" />}
                {a.kind === "video" && a.url && <video src={a.url} controls className="w-full rounded-lg" />}
                {(a.kind === "audio" || a.kind === "music") && a.url && /\.(png|jpe?g|webp|gif)(\?|$)/i.test(a.url) && (
                  <img src={a.url} alt={a.title} className="w-full rounded-lg" />
                )}
                {(a.kind === "audio" || a.kind === "music") && a.url && !/\.(png|jpe?g|webp|gif)(\?|$)/i.test(a.url) && (
                  <audio src={a.url} controls preload="metadata" className="w-full" />
                )}
                {!a.url && <div className="text-xs text-zinc-600">{a.title}</div>}
              </Card>
            ))}
        </div>
      </section>

      <Card className="flex max-h-[78vh] flex-col p-4">
        <div className="text-xs text-zinc-500">Чат с оркестратором</div>
        <div ref={chatLog} className="mt-3 flex-1 space-y-3 overflow-auto">
          {(p.messages || []).map((m) => (
            <div key={m.id} className="text-sm">
              <div className="text-[11px] uppercase text-zinc-600">{m.role}</div>
              <p className="whitespace-pre-wrap text-zinc-300">{m.content}</p>
            </div>
          ))}
        </div>
        {error && <p className="mt-2 text-[12px] text-red-400">{error}</p>}
        <div className="mt-3 flex gap-2">
          <Input
            value={chat}
            disabled={busy}
            onChange={(e) => setChat(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            placeholder="Уточни сцену, стиль, персонажа…"
          />
          <Button disabled={busy} onClick={send}>
            {busy ? "…" : "OK"}
          </Button>
        </div>
        <div className="mt-4 text-[11px] leading-relaxed text-zinc-600">
          Платформа: {p.platform}. Контекст проекта (план, модели, чат) сохраняется. 3D — каталог, медиа — Kie.ai.
        </div>
      </Card>
    </main>
    </>
  );
}
