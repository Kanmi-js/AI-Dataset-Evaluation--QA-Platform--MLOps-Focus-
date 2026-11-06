import React, { useCallback, useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { Dataset } from '../api/types';

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export default function DashboardPage() {
  const [tasks, setTasks] = useState<Dataset[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [activeFilter, setActiveFilter] = useState<'all' | 'ready'>('all');
  const [isModalOpen, setIsModalOpen] = useState<boolean>(false);
  const [newTaskName, setNewTaskName] = useState<string>('');
  const [newTaskDesc, setNewTaskDesc] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // useCallback so the effect below can depend on it honestly rather than
  // suppressing exhaustive-deps (which is at error level, by project policy).
  const fetchTasks = useCallback(async () => {
    try {
      setLoading(true);
      const data = await api.listDatasets();
      setTasks(Array.isArray(data) ? data : []);
      setError(null);
    } catch (err) {
      setError(errorMessage(err, 'Could not load tasks'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchTasks();
  }, [fetchTasks]);

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTaskName.trim() || isSubmitting) return;

    try {
      setIsSubmitting(true);
      await api.createDataset(newTaskName, newTaskDesc);
      setIsModalOpen(false);
      setNewTaskName('');
      setNewTaskDesc('');
      await fetchTasks();
    } catch (err) {
      setError(errorMessage(err, 'Failed to create task'));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleDeleteTask = async (e: React.MouseEvent, datasetId: number) => {
    e.preventDefault();
    e.stopPropagation();

    if (!window.confirm('Are you sure you want to delete this task?')) return;

    try {
      await api.deleteDataset(datasetId);
      setTasks((prev) => prev.filter((t) => t.id !== datasetId));
    } catch (err) {
      setError(errorMessage(err, 'Could not delete task'));
    }
  };

  const filteredTasks = tasks.filter((t) => {
    const matchesSearch =
      t.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (t.description && t.description.toLowerCase().includes(searchQuery.toLowerCase()));

    if (activeFilter === 'ready') {
      return matchesSearch && (t.item_count ?? 0) > 0;
    }
    return matchesSearch;
  });

  // Every figure on the cards below is derived from the datasets actually
  // returned by the API. Nothing here is a placeholder percentage.
  const totalItems = tasks.reduce((acc, t) => acc + (t.item_count ?? 0), 0);
  const populated = tasks.filter((t) => (t.item_count ?? 0) > 0).length;
  const populatedPct = tasks.length ? Math.round((populated / tasks.length) * 100) : 0;

  return (
    <div className="min-h-screen bg-pitch-dark text-zinc-100 p-6 md:p-10 font-sans selection:bg-volt selection:text-pitch-dark">
      {/* Top Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-pitch-border pb-8">
        <div>
          <div className="inline-flex items-center gap-2 px-2.5 py-1 rounded-full bg-volt/10 border border-volt/30 text-volt text-xs font-mono mb-3 tracking-wide">
            <span className="w-1.5 h-1.5 rounded-full bg-volt animate-pulse" />
            TELEMETRY &amp; TACTICAL ENGINE
          </div>
          <h1 className="text-3xl md:text-4xl font-bold tracking-tight">
            TerrainAI <span className="text-zinc-500 font-light">/</span> <span className="text-white">Tasks</span>
          </h1>
          <p className="text-zinc-400 text-sm mt-1 max-w-2xl font-normal">
            Upload match stills and clips, let the worker pre-process them, then
            review and annotate the results. Clip tracking is simulated — see
            the README before treating any figure as scouting data.
          </p>
        </div>

        <button
          type="button"
          onClick={() => setIsModalOpen(true)}
          className="inline-flex items-center justify-center gap-2 bg-volt text-pitch-dark hover:bg-volt-dim font-semibold px-5 py-2.5 rounded-lg shadow-volt-glow transition-all duration-200 text-sm self-start md:self-auto cursor-pointer"
        >
          <span className="text-lg leading-none">+</span> Create Task
        </button>
      </div>

      {error ? (
        <div className="mt-6 flex items-center gap-2 rounded-lg border border-vector-crimson/30 bg-vector-crimson/10 px-4 py-2.5 font-mono text-xs text-vector-crimson">
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="ml-auto cursor-pointer text-zinc-400 hover:text-white"
          >
            dismiss
          </button>
        </div>
      ) : null}

      {/* Metric cards — every figure derived from the API response */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-5 mt-8">
        <div className="bg-pitch-card border border-pitch-border rounded-xl p-5 relative overflow-hidden group hover:border-volt/40 transition-all">
          <div className="absolute top-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent via-volt/40 to-transparent" />
          <div className="flex items-center justify-between text-zinc-400 text-xs font-mono uppercase tracking-wider">
            <span>ACTIVE TASKS</span>
            <span className="text-volt text-base">◫</span>
          </div>
          <div className="flex items-baseline gap-3 mt-4">
            <span className="text-3xl font-bold font-mono text-white tracking-tight">{tasks.length}</span>
          </div>
          <div className="w-full bg-pitch-dark h-1.5 rounded-full mt-4 overflow-hidden border border-pitch-border">
            <div
              className="bg-volt h-full rounded-full transition-all"
              style={{ width: `${populatedPct}%` }}
            />
          </div>
          <p className="text-[11px] text-zinc-500 font-mono mt-2">
            {populated} of {tasks.length} hold at least one item
          </p>
        </div>

        <div className="bg-pitch-card border border-pitch-border rounded-xl p-5 relative overflow-hidden group hover:border-vector-cyan/40 transition-all">
          <div className="absolute top-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent via-vector-cyan/40 to-transparent" />
          <div className="flex items-center justify-between text-zinc-400 text-xs font-mono uppercase tracking-wider">
            <span>ITEMS UPLOADED</span>
            <span className="text-vector-cyan text-base">⬡</span>
          </div>
          <div className="flex items-baseline gap-3 mt-4">
            <span className="text-3xl font-bold font-mono text-white tracking-tight">
              {totalItems}
            </span>
            <span className="text-xs font-mono text-vector-cyan bg-vector-cyan/10 border border-vector-cyan/20 px-1.5 py-0.5 rounded">
              Stills &amp; clips
            </span>
          </div>
          <p className="text-xs text-zinc-400 mt-4">Across every task you own</p>
        </div>

        <div className="bg-pitch-card border border-pitch-border rounded-xl p-5 relative overflow-hidden group hover:border-volt/40 transition-all">
          <div className="absolute top-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent via-volt/40 to-transparent" />
          <div className="flex items-center justify-between text-zinc-400 text-xs font-mono uppercase tracking-wider">
            <span>TRACKING ANALYSIS</span>
            <span className="text-vector-amber text-base">◎</span>
          </div>
          <div className="flex items-baseline gap-3 mt-4">
            <span className="text-2xl font-bold font-mono text-vector-amber tracking-tight">
              Simulated
            </span>
          </div>
          <p className="text-xs text-zinc-400 mt-4">
            Clip properties are measured; trajectories come from a kinematic
            model, not from the footage.
          </p>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className="flex flex-col sm:flex-row items-center justify-between gap-4 mt-8">
        <div className="relative w-full sm:w-80 md:w-96">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search tasks by title, tactics, or tags..."
            className="w-full bg-pitch-card border border-pitch-border rounded-lg px-3.5 py-2 text-xs md:text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-volt transition-colors font-mono"
          />
        </div>

        <div className="flex items-center gap-2 self-start sm:self-auto font-mono text-xs">
          <button
            type="button"
            onClick={() => setActiveFilter('all')}
            className={`px-3 py-1.5 rounded-lg border transition-all cursor-pointer ${
              activeFilter === 'all'
                ? 'border-volt bg-volt/10 text-volt font-semibold shadow-volt-glow'
                : 'border-pitch-border bg-pitch-card text-zinc-400 hover:text-zinc-200 hover:border-zinc-700'
            }`}
          >
            All Sets
          </button>
          <button
            type="button"
            onClick={() => setActiveFilter('ready')}
            className={`px-3 py-1.5 rounded-lg border transition-all cursor-pointer ${
              activeFilter === 'ready'
                ? 'border-volt bg-volt/10 text-volt font-semibold shadow-volt-glow'
                : 'border-pitch-border bg-pitch-card text-zinc-400 hover:text-zinc-200 hover:border-zinc-700'
            }`}
          >
            Ready
          </button>
          <span className="px-3 py-1.5 rounded-lg bg-pitch-card border border-pitch-border text-zinc-500">
            {filteredTasks.length} {filteredTasks.length === 1 ? 'Task' : 'Tasks'}
          </span>
        </div>
      </div>

      {/* Tasks Grid */}
      <div className="mt-6">
        {loading ? (
          <div className="py-12 text-center text-zinc-500 font-mono text-sm">
            Scanning tactical tasks...
          </div>
        ) : filteredTasks.length === 0 ? (
          <div className="p-8 text-center border border-dashed border-pitch-border rounded-xl text-zinc-500 font-mono text-xs">
            No tasks found. Click "+ Create Task" above to initialize a session.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {filteredTasks.map((ds) => (
              <Link
                key={ds.id}
                to={`/tasks/${ds.id}`}
                className="group relative flex flex-col justify-between overflow-hidden rounded-xl border border-pitch-border bg-pitch-card p-5 hover:border-volt/50 transition-all duration-200"
              >
                <div className="absolute top-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent via-volt/50 to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />

                <div>
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-center gap-2.5">
                      <div className="grid h-8 w-8 place-items-center rounded-lg bg-volt/10 border border-volt/20 text-volt font-mono text-xs font-bold">
                        {ds.name.substring(0, 2).toUpperCase()}
                      </div>
                      <h3 className="font-bold text-base text-zinc-100 group-hover:text-volt transition-colors">
                        {ds.name}
                      </h3>
                    </div>

                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs px-2 py-0.5 rounded bg-pitch-dark border border-pitch-border text-zinc-400">
                        {ds.item_count ?? 0} items
                      </span>
                      <button
                        type="button"
                        title="Delete task"
                        onClick={(e) => handleDeleteTask(e, ds.id)}
                        className="px-2 py-0.5 rounded text-zinc-500 hover:text-red-400 hover:bg-red-500/10 border border-transparent hover:border-red-500/30 transition-all cursor-pointer font-mono text-xs"
                      >
                        ✕
                      </button>
                    </div>
                  </div>

                  <p className="mt-3 text-xs text-zinc-400 line-clamp-2 font-normal">
                    {ds.description || 'Tactical match analysis and automated bounding box telemetry.'}
                  </p>
                </div>

                <div className="mt-6 flex items-center justify-between border-t border-pitch-border/60 pt-4 font-mono text-xs text-zinc-500">
                  <span>{ds.created_at ? new Date(ds.created_at).toLocaleDateString() : 'Active Session'}</span>
                  <span className="text-volt group-hover:translate-x-0.5 transition-transform inline-flex items-center gap-1 font-semibold">
                    Open Workspace &gt;
                  </span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>

      {/* Create Task Modal */}
      {isModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4">
          <div className="bg-pitch-card border border-pitch-border w-full max-w-md rounded-xl p-6 shadow-2xl relative">
            <div className="flex items-center justify-between border-b border-pitch-border pb-4">
              <h3 className="text-lg font-bold text-white tracking-tight flex items-center gap-2">
                <span className="text-volt">●</span> Create Tactical Task
              </h3>
              <button
                type="button"
                onClick={() => setIsModalOpen(false)}
                className="text-zinc-500 hover:text-zinc-300 text-sm font-mono cursor-pointer"
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleCreateTask} className="mt-4 space-y-4">
              <div>
                <label className="block text-xs font-mono text-zinc-400 uppercase tracking-wider mb-1.5">
                  Task Name
                </label>
                <input
                  type="text"
                  required
                  value={newTaskName}
                  onChange={(e) => setNewTaskName(e.target.value)}
                  placeholder="e.g., Match Analysis 4-3-3 Transition"
                  className="w-full bg-pitch-dark border border-pitch-border rounded-lg px-3.5 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-volt font-mono transition-colors"
                />
              </div>

              <div>
                <label className="block text-xs font-mono text-zinc-400 uppercase tracking-wider mb-1.5">
                  Tactical Notes / Description
                </label>
                <textarea
                  rows={3}
                  value={newTaskDesc}
                  onChange={(e) => setNewTaskDesc(e.target.value)}
                  placeholder="Player movement tracking, pressing intensity, and spatial positioning..."
                  className="w-full bg-pitch-dark border border-pitch-border rounded-lg px-3.5 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-volt text-xs transition-colors"
                />
              </div>

              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setIsModalOpen(false)}
                  className="px-4 py-2 rounded-lg border border-pitch-border text-zinc-400 hover:text-zinc-200 text-xs font-mono transition-colors cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isSubmitting}
                  className="px-4 py-2 rounded-lg bg-volt text-pitch-dark hover:bg-volt-dim text-xs font-semibold font-mono shadow-volt-glow transition-all disabled:opacity-50 cursor-pointer"
                >
                  {isSubmitting ? 'Creating...' : '+ Initialize Task'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}