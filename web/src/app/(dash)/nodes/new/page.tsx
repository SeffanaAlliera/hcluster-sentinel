import { requireAdminPage } from '@/lib/auth';
import { AddNodeForm } from '@/components/AddNodeForm';

export default async function NewNode() {
  await requireAdminPage();
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-white">Add node</h1>
      <p className="max-w-xl text-sm text-slate-400">Creates the node record and a unique API token. The agent on that machine uses the token to authenticate; nothing is exposed on your home network.</p>
      <AddNodeForm />
    </div>
  );
}
