import { useMemo, useState } from 'react';
import { api } from '../api/client';
import type { DownloadItemOut } from '../api/types';
import { Icon } from '../components/Icon';
import { Button, Cover, Empty, ErrorState, IconButton, PageHeader, ProgressBar, Segmented, SkeletonRows, StatusChip, cx } from '../components/ui';
import { useHubConnected, useHubEvent } from '../lib/events';
import { DOWNLOAD_ACTIVE, fmtWhen, providerLabel, warningText } from '../lib/format';
import { useAsync, useInterval } from '../lib/hooks';
import { canPlay, playList } from '../lib/player';
import { Link } from '../lib/router';
import { attempt, toast } from '../lib/ui';

type Filter = 'all' | 'active' | 'problems';

export function DownloadsPage() {
  const res = useAsync((s) => api.downloads(undefined, 200, s), []);
  const connected = useHubConnected();
  const [filter, setFilter] = useState<Filter>('all');
  const [clearing, setClearing] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  // Live updates: upsert the item; unknown items (new batches) are prepended.
  useHubEvent('download', (d) => {
    res.setData((prev) => {
      if (!prev) return prev;
      const i = prev.findIndex((x) => x.id === d.id);
      if (i < 0) return [d, ...prev];
      const next = prev.slice();
      next[i] = { ...prev[i], ...d, candidates: prev[i].candidates };
      return next;
    });
  });
  useHubEvent('reconnect', () => res.reload(true));

  // downloads queued only for ♥ or by an import are the Hub's own business: not shown (an import has its card); a ♥
  // download that failed comes with `star` false and is shown
  const imports = useAsync((s) => api.imports(s), []);
  useHubEvent('import', () => imports.reload(true));
  const running = (imports.data ?? []).filter((x) => x.status === 'running');
  useInterval(() => imports.reload(true), running.length ? 15_000 : null);
  const items = useMemo(() => (res.data ?? []).filter((x) =>
    !x.star || ((x.mix_id || x.wave_session_id) && x.status !== 'done' && x.status !== 'cancelled'),
  ), [res.data]);
  const counts = useMemo(
    () => ({
      active: items.filter((x) => DOWNLOAD_ACTIVE.includes(x.status)).length,
      problems: items.filter((x) => x.status === 'failed' || x.status === 'needs_review').length,
      finished: items.filter((x) => x.status === 'done' || x.status === 'cancelled').length,
    }),
    [items],
  );
  const shown = items.filter((x) =>
    filter === 'active' ? DOWNLOAD_ACTIVE.includes(x.status) : filter === 'problems' ? x.status === 'failed' || x.status === 'needs_review' : true,
  );

  const clear = async () => {
    const problems = filter === 'problems';
    setClearing(true);
    const r = await attempt(() => api.clearDownloads(problems));
    setClearing(false);
    if (r) {
      toast(r.removed ? `Убрано из списка: ${r.removed}` : 'Нечего очищать', 'success');
      res.setData((prev) => prev?.filter((x) =>
        problems ? x.status !== 'failed' && x.status !== 'needs_review' : x.status !== 'done' && x.status !== 'cancelled',
      ));
    }
  };

  const act = async (item: DownloadItemOut, kind: 'retry' | 'cancel') => {
    setBusyId(item.id);
    const r = await attempt(() => (kind === 'retry' ? api.retry(item.id) : api.cancel(item.id)));
    setBusyId(null);
    if (r) res.setData((prev) => prev?.map((x) => (x.id === r.id ? { ...x, ...r } : x)));
  };

  return (
    <div>
      <PageHeader
        title="Загрузки"
        sub={
          <span className="inline-flex items-center gap-1.5">
            <span className={cx('h-2 w-2 rounded-full', res.error ? 'bg-danger' : connected ? 'bg-accent' : 'bg-dim')} />
            {res.error ? 'нет связи' : connected ? 'онлайн' : 'переподключаюсь…'}
          </span>
        }
        right={
          <Button small variant="ghost" busy={clearing} disabled={filter === 'problems' ? !counts.problems : !counts.finished} onClick={clear}>
            {filter === 'problems' ? 'Очистить' : 'Очистить готовые'}
          </Button>
        }
      />
      {running.map((imp) => (
        <Link key={imp.id} to={`/import/${imp.id}`} className="card mx-4 mb-3 flex items-center gap-3 p-3 hover:bg-hover">
          <Icon name="transfer" size={22} className="shrink-0 text-accent" />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-semibold">{imp.source === 'spotify' ? 'Перенос из Spotify' : 'Перенос из Яндекс Музыки'}</span>
            <span className="block truncate text-xs text-muted">
              {imp.stats.pending ? `сопоставляем · осталось ${imp.stats.pending}` : `скачано ${imp.stats.done} · качается ${imp.stats.downloading}`}
            </span>
          </span>
          <Icon name="chevron" size={18} className="text-dim" />
        </Link>
      ))}
      <div className="px-4 pb-2">
        <Segmented<Filter>
          label="Фильтр"
          value={filter}
          onChange={setFilter}
          options={[
            { value: 'all', label: `Все ${items.length || ''}`.trim() },
            { value: 'active', label: `Активные ${counts.active || ''}`.trim() },
            { value: 'problems', label: `Проблемы ${counts.problems || ''}`.trim() },
          ]}
        />
      </div>

      {res.loading ? (
        <SkeletonRows />
      ) : res.error ? (
        <ErrorState text={res.error} onRetry={() => res.reload()} />
      ) : shown.length === 0 ? (
        <Empty icon="download" title={filter === 'all' ? 'Загрузок пока нет' : 'Здесь пусто'} text={filter === 'all' ? 'Найдите трек или альбом в поиске и нажмите «Скачать».' : undefined}>
          {filter === 'all' && (
            <Link to="/search" className="mt-2 rounded-full bg-fg px-5 py-2.5 font-semibold text-bg">
              Перейти к поиску
            </Link>
          )}
        </Empty>
      ) : (
        <ul className="divide-y divide-line/60">
          {shown.map((item) => (
            <DownloadRow
              key={item.id}
              item={item}
              busy={busyId === item.id}
              onRetry={() => act(item, 'retry')}
              onCancel={() => act(item, 'cancel')}
              onPlay={canPlay() && item.status === 'done' && item.nd_song_id ? () => playList(shown.filter((x) => x.status === 'done'), (x) => x.nd_song_id, item) : undefined}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function DownloadRow({
  item,
  busy,
  onRetry,
  onCancel,
  onPlay,
}: {
  item: DownloadItemOut;
  busy: boolean;
  onRetry: () => void;
  onCancel: () => void;
  /** Done and scanned by Navidrome: plays it (and the finished ones around it) in the app's player. */
  onPlay?: () => void;
}) {
  const active = DOWNLOAD_ACTIVE.includes(item.status);
  const problem = item.status === 'failed' || item.status === 'needs_review';
  const warnings = (item.warnings ?? []).map(warningText);
  const cover = item.chosen?.cover_url;
  return (
    <li className="flex items-start gap-3 py-3 pl-4 pr-1">
      <Link to={`/downloads/${item.id}`} className="flex min-w-0 flex-1 gap-3" aria-label={`Подробности: ${item.artist} — ${item.title}`}>
        <div className="relative shrink-0">
          <Cover src={cover} size={48} alt="" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px]">{item.title}</div>
          <div className="truncate text-[13px] text-muted">{[item.artist, item.album].filter(Boolean).join(' · ')}</div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-dim">
            <StatusChip status={item.waiting_for_soulseek ? 'waiting_soulseek' : item.status} progress={item.progress} />
            {item.liked && item.status !== 'done' && <span className="text-accent">♥ после загрузки</span>}
            {item.provider && !item.waiting_for_soulseek && <span>{providerLabel(item.provider)}{item.audio_format ? ` ${item.audio_format.toUpperCase()}` : ''}</span>}
            {item.mix_id && <span>Микс дня</span>}
            {item.wave_session_id && <span>Моя волна</span>}
            <span>{fmtWhen(item.updated_at)}</span>
          </div>
          {item.status === 'downloading' && <ProgressBar value={item.progress} className="mt-2" />}
          {warnings.map((w, i) => (
            <div key={i} className="mt-1 flex items-center gap-1 text-xs text-warn">
              <Icon name="alert" size={13} />
              {w}
            </div>
          ))}
          {problem && item.error && <div className="mt-1 line-clamp-2 text-xs text-danger">{item.error}</div>}
          {item.status === 'needs_review' && <div className="mt-1 text-xs font-medium text-warn">Нажмите, чтобы выбрать файл вручную</div>}
        </div>
      </Link>
      <div className="flex shrink-0 flex-col items-center">
        {active ? (
          <IconButton icon="x" label="Отменить" busy={busy} onClick={onCancel} className="text-muted" />
        ) : problem || item.status === 'cancelled' ? (
          <IconButton icon="refresh" label="Повторить" busy={busy} onClick={onRetry} className="text-muted" />
        ) : onPlay ? (
          <IconButton icon="play" label={`Слушать «${item.title}»`} onClick={onPlay} className="!text-accent" />
        ) : item.status === 'done' ? (
          <span className="flex h-11 w-11 items-center justify-center text-accent" aria-label="Готово">
            <Icon name="check" size={20} stroke={3} />
          </span>
        ) : null}
      </div>
    </li>
  );
}
