import { NavLink, Route, Routes } from "react-router-dom";
import Home from "./pages/Home";
import Project from "./pages/Project";

export default function App() {
  return (
    <div className="min-h-full bg-[radial-gradient(circle_at_top,_#1b1433,_#09090b_45%)]">
      <header className="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/70 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <NavLink to="/" className="text-sm font-semibold tracking-wide">
            MODELFOLDER <span className="text-violet-400">STUDIO</span>
          </NavLink>
          <a className="text-xs text-zinc-500 hover:text-zinc-300" href="/">
            ← каталог моделей
          </a>
        </div>
      </header>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/projects/:id" element={<Project />} />
      </Routes>
    </div>
  );
}
