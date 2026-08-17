import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Badge, Card, Textarea } from "@/components/ui/primitives";
import { api } from "@/lib/utils";
import { Clapperboard, Gamepad2, Smartphone, Monitor } from "lucide-react";

type Project = {
  id: string;
  title: string;
  prompt: string;
  platform: string;
  status: string;
  created_at: string;
};

const platforms = [
  { id: "web", label: "Веб", icon: Gamepad2 },
  { id: "mobile", label: "Мобильное", icon: Smartphone },
  { id: "desktop", label: "Десктоп", icon: Monitor },
];

export default function Home() {
  const nav = useNavigate();
  const [prompt, setPrompt] = useState("");
  const [platform, setPlatform] = useState("web");
  const [busy, setBusy] = useState(false);
  const [items, setItems] = useState<Project[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ items: Project[] }>("/projects")
      .then((d) => setItems(d.items || []))
      .catch(() => setItems([]));
  }, []);

  async function create() {
    setBusy(true);
    setError("");
    try {
      const p = await api<Project>("/projects", {
        method: "POST",
        body: JSON.stringify({ prompt, platform }),
      });
      nav(`/projects/${p.id}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto grid max-w-7xl gap-8 px-6 py-10 lg:grid-cols-[1.2fr_0.8fr]">
      <section>
        <Badge>облачная студия</Badge>
        <h1 className="mt-4 max-w-2xl text-4xl font-semibold leading-tight">
          ИИ оркестрирует агентов и собирает игру: модели, видео, голос, саундтрек.
        </h1>
        <p className="mt-3 max-w-xl text-sm text-zinc-400">
          Запрос уходит оркестратору (Claude / Grok / GPT через Kie.ai), тот подбирает 3D из каталога
          ModelFolder и запускает генерацию картинки, ролика, озвучки и музыки.
        </p>
        <Card className="mt-8 p-5">
          <Textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Опиши только эту игру: жанр, герой, мир. Каждый проект — отдельный контекст."
          />
          <div className="mt-4 flex flex-wrap gap-2">
            {platforms.map((p) => (
              <Button
                key={p.id}
                type="button"
                variant={platform === p.id ? "default" : "ghost"}
                onClick={() => setPlatform(p.id)}
              >
                <p.icon className="h-4 w-4" />
                {p.label}
              </Button>
            ))}
          </div>
          {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
          <Button className="mt-5" size="lg" disabled={busy || !prompt.trim()} onClick={create}>
            <Clapperboard className="h-4 w-4" />
            {busy ? "Запускаем пайплайн…" : "Собрать игру"}
          </Button>
        </Card>
      </section>
      <aside className="space-y-3">
        <h2 className="text-sm text-zinc-500">Недавние проекты</h2>
        {items.length === 0 && <p className="text-sm text-zinc-600">Пока пусто</p>}
        {items.map((p) => (
          <button
            key={p.id}
            onClick={() => nav(`/projects/${p.id}`)}
            className="block w-full rounded-xl border border-zinc-800 bg-zinc-950 p-4 text-left hover:border-violet-500/40"
          >
            <div className="flex items-center justify-between gap-3">
              <div className="font-medium">{p.title || "Без названия"}</div>
              <Badge>{p.status}</Badge>
            </div>
            <p className="mt-1 line-clamp-2 text-xs text-zinc-500">{p.prompt}</p>
          </button>
        ))}
      </aside>
    </main>
  );
}
