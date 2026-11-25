import { LoginForm } from './form';
export default function LoginPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4">
      <div className="mb-6 text-center">
        <div className="text-2xl font-semibold tracking-tight text-white">hCluster <span className="text-sky-400">Sentinel</span></div>
        <p className="mt-1 text-sm text-slate-400">Sign in to view your homelab</p>
      </div>
      <LoginForm />
    </main>
  );
}
