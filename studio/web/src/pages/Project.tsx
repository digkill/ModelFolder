import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api, apiBase } from "@/lib/utils";
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
};
type Asset = { id: string; kind: string; title: string; url: string; meta?: Record<string, unknown> };
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

  const modelUrl = useMemo(() => {
    const mesh = p?.assets.find((a) => a.kind === "mesh" && a.url);
    const model = p?.assets.find((a) => a.kind === "model" && a.url);
    return mesh?.url || model?.url;
  }, [p]);

  async function send() {
    if (!id || !chat.trim()) return;
    setBusy(true);
    try {
      await api(`/projects/${id}/chat`, { method: "POST", body: JSON.stringify({ message: chat }) });
      setChat("");
      await reload();
    } finally {
      setBusy(false);
    }
  }

  if (!p) return <div className="p-10 text-zinc-500">Загрузка…</div>;

  return (
    <main className="mx-auto grid max-w-[1400px] gap-4 px-4 py-6 lg:grid-cols-[320px_1fr_340px]">
      <Card className="flex max-h-[78vh] flex-col p-4">
        <div className="text-xs text-zinc-500">Пайплайн агентов</div>
        <h2 className="mt-1 text-lg font-semibold">{p.title}</h2>
        <Badge className="mt-2 w-fit">{p.status}</Badge>
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
            <div className="text-xs text-zinc-500">Превью сцены</div>
            <div className="flex gap-2">
              <Button size="sm" variant={engine === "three" ? "default" : "ghost"} onClick={() => setEngine("three")}>
                Three.js
              </Button>
              <Button size="sm" variant={engine === "babylon" ? "default" : "ghost"} onClick={() => setEngine("babylon")}>
                Babylon.js
              </Button>
            </div>
          </div>
          <div className="h-[calc(52vh-48px)]">
            <Viewport url={modelUrl} engine={engine} />
          </div>
        </Card>
        <div className="grid gap-3 md:grid-cols-2">
          {(p.assets || [])
            .filter((a) => a.kind === "image" || a.kind === "video" || a.kind === "audio" || a.kind === "music")
            .map((a) => (
              <Card key={a.id} className="overflow-hidden p-3">
                <div className="mb-2 text-xs text-zinc-500">{a.kind}</div>
                {a.kind === "image" && a.url && <img src={a.url} alt={a.title} className="w-full rounded-lg" />}
                {a.kind === "video" && a.url && <video src={a.url} controls className="w-full rounded-lg" />}
                {(a.kind === "audio" || a.kind === "music") && a.url && (
                  <audio src={a.url} controls className="w-full" />
                )}
                {!a.url && <div className="text-xs text-zinc-600">{a.title}</div>}
              </Card>
            ))}
        </div>
      </section>

      <Card className="flex max-h-[78vh] flex-col p-4">
        <div className="text-xs text-zinc-500">Чат с оркестратором</div>
        <div className="mt-3 flex-1 space-y-3 overflow-auto">
          {(p.messages || []).map((m) => (
            <div key={m.id} className="text-sm">
              <div className="text-[11px] uppercase text-zinc-600">{m.role}</div>
              <p className="whitespace-pre-wrap text-zinc-300">{m.content}</p>
            </div>
          ))}
        </div>
        <div className="mt-3 flex gap-2">
          <Input value={chat} onChange={(e) => setChat(e.target.value)} placeholder="Уточни сцену, стиль, персонажа…" />
          <Button disabled={busy} onClick={send}>
            OK
          </Button>
        </div>
        <div className="mt-4 text-[11px] leading-relaxed text-zinc-600">
          Платформа: {p.platform}. Модели из каталога подтягиваются семантическим поиском, медиа — через Kie.ai,
          3D-скульпт — Meshy.
        </div>
      </Card>
    </main>
  );
}
