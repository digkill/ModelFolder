import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { Engine, Scene, ArcRotateCamera, HemisphericLight, Vector3, Color4 } from "@babylonjs/core";
import "@babylonjs/loaders/glTF";
import { SceneLoader } from "@babylonjs/core/Loading/sceneLoader";

export function Viewport({ url, engine }: { url?: string; engine: "three" | "babylon" }) {
  if (engine === "babylon") return <BabylonView url={url} />;
  return <ThreeView url={url} />;
}

function ThreeView({ url }: { url?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
    camera.position.set(2.4, 1.6, 3.2);
    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x334, 1.2));
    const dir = new THREE.DirectionalLight(0xffffff, 1.1);
    dir.position.set(4, 8, 2);
    scene.add(dir);
    const grid = new THREE.GridHelper(8, 16, 0x4c1d95, 0x27272a);
    scene.add(grid);
    let frame = 0;
    const resize = () => {
      const w = canvas.clientWidth || 640;
      const h = canvas.clientHeight || 400;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas.parentElement || canvas);
    const tick = () => {
      frame = requestAnimationFrame(tick);
      controls.update();
      renderer.render(scene, camera);
    };
    tick();
    let root: THREE.Object3D | null = null;
    if (url) {
      new GLTFLoader().load(url, (gltf) => {
        root = gltf.scene;
        scene.add(root);
        const box = new THREE.Box3().setFromObject(root);
        const size = box.getSize(new THREE.Vector3()).length() || 1;
        root.scale.multiplyScalar(2 / size);
      });
    }
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      if (root) scene.remove(root);
    };
  }, [url]);
  return <canvas ref={ref} className="h-full w-full rounded-xl" />;
}

function BabylonView({ url }: { url?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const key = useMemo(() => url || "", [url]);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const engine = new Engine(canvas, true, { adaptToDeviceRatio: true });
    const scene = new Scene(engine);
    scene.clearColor = new Color4(0.04, 0.04, 0.06, 1);
    const camera = new ArcRotateCamera("cam", 1.1, 1.1, 6, Vector3.Zero(), scene);
    camera.attachControl(canvas, true);
    new HemisphericLight("h", new Vector3(0, 1, 0), scene);
    if (key) {
      SceneLoader.Append("", key, scene, undefined, undefined, undefined, ".glb");
    }
    engine.runRenderLoop(() => scene.render());
    const onResize = () => engine.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      engine.dispose();
    };
  }, [key]);
  return <canvas ref={ref} className="h-full w-full rounded-xl" />;
}
