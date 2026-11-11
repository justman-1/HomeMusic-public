import { useEffect, useMemo, useState } from 'react';
import { ApiError, api } from '../api/client';
import type { CatalogArtist, MixMode, MixOut, MixSettingsOut, MixTodayOut, MixTrackOut } from '../api/types';
import { TrackRow, listenTrack } from '../components/catalog';
import { Icon } from '../components/Icon';
import { Button, Cover, ErrorState, IconButton, PageHeader, ProgressBar, SkeletonRows, Spinner, cx } from '../components/ui';
import { DOWNLOAD_ACTIVE, downloadStatusLabel, fmtLength, plural, providerLabel } from '../lib/format';
import { useHubEvent } from '../lib/events';
import { useAsync, useDebounced, useInterval } from '../lib/hooks';
import { listen } from '../lib/listen';
import { canPlay, canPlayAll, playList, playTracks } from '../lib/player';
import { Link, goBack, navigate } from '../lib/router';
import { useSession } from '../lib/session';
import { attempt, toast } from '../lib/ui';

/** «Микс дня» (MIXES.md 6.4): the card at the top of the catalog, the mix's page and «Что вы слушаете?» for a user
 * without history. Everything comes from `GET /api/mixes/today`; songs that are not in the library yet download
 * quietly, so the page asks again while they do. */

const MODES: Array<{ value: MixMode; label: string; hint: string }> = [
  { value: 'normal', label: 'Обычный', hint: '40% знакомого' },
  { value: 'favorites', label: 'Любимое', hint: '80% знакомого' },
  { value: 'discover', label: 'Незнакомое', hint: '90% нового' },
];

const POLL_MS = 8000;
const BUILD_POLL_MS = 4000;
const PICK_MIN = 3;
const PICK_MAX = 5;

const songsWord = (n: number) => `${n} ${plural(n, ['песня', 'песни', 'песен'])}`;
const mixContext = (id: number) => `mix:${id}`;

/** Up to four different covers of the mix, 2×2; a single cover when there are fewer. Small covers (250 px) unless
 * `large`: the big single cover of the mix's own screen. */
export function MixCover({ tracks, className, large }: { tracks: readonly MixTrackOut[]; className?: string; large?: boolean }) {
  const covers = useMemo(() => {
    const small = [...new Set(tracks.map((t) => t.cover_small_url || t.cover_url).filter((c): c is string => !!c))];
    const big = large ? [...new Set(tracks.map((t) => t.cover_url).filter((c): c is string => !!c))] : [];
    return (small.length < 4 && big.length ? big : small).slice(0, 4);
  }, [tracks, large]);
  if (covers.length < 4)
    return covers.length ? (
      <Cover src={covers[0]} alt="" className={className} />
    ) : (
      <div className={cx('flex items-center justify-center rounded-md bg-gradient-to-br from-accent/60 to-accent/10 text-on-accent', className)}>
        <Icon name="sparkle" size={28} />
      </div>
    );
  return (
    <div className={cx('grid shrink-0 grid-cols-2 grid-rows-2 overflow-hidden rounded-md bg-raised', className)} aria-hidden>
      {covers.map((c) => (
        <img key={c} src={c} alt="" loading="lazy" decoding="async" referrerPolicy="no-referrer" className="h-full w-full object-cover" />
      ))}
    </div>
  );
}

/** Top of the catalog: today's mix, the build under way, or «Что вы слушаете?». */
export function MixCard() {
  const res = useAsync((s) => api.mixToday(s), []);
  const today = res.data;
  useInterval(() => res.reload(true), today && (today.building || (today.mix && today.mix.ready < today.mix.total)) ? POLL_MS : null);
  if (!today) return null;
  const { mix, building, onboarding, settings } = today;

  if (mix)
    return (
      <Link to={`/mix/${mix.id}`} className="mx-4 mt-3 flex items-center gap-3 rounded-xl bg-raised p-3 transition hover:bg-hover" aria-label={`Микс дня: ${mix.title}`}>
        <MixCover tracks={mix.tracks} className="h-20 w-20" />
        <div className="min-w-0 flex-1">
          <div className="line-clamp-2 font-bold leading-tight">{mix.title}</div>
          <div className="mt-1 text-xs text-dim">
            {mix.ready < mix.total ? `${mix.ready} из ${mix.total} в библиотеке` : songsWord(mix.total)}
          </div>
        </div>
        <Icon name="chevron" size={20} className="shrink-0 text-dim" />
      </Link>
    );

  if (building)
    return (
      <div className="mx-4 mt-3 flex items-center gap-3 rounded-xl bg-raised p-4" role="status">
        <Spinner className="text-accent" />
        <div className="min-w-0">
          <div className="font-bold">Собираем «Микс дня»</div>
          <div className="text-[13px] text-muted">Подбираем 50 песен по вашему вкусу — это займёт пару минут</div>
        </div>
      </div>
    );

  if (onboarding)
    return (
      <Link to="/taste" className="mx-4 mt-3 flex items-center gap-3 rounded-xl bg-accent/15 p-4 transition hover:bg-accent/20">
        <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-accent text-on-accent">
          <Icon name="sparkle" size={24} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="font-bold">Что вы слушаете?</div>
          <div className="text-[13px] text-muted">Выберите 3–5 исполнителей — соберём для вас «Микс дня»</div>
        </div>
        <Icon name="chevron" size={20} className="shrink-0 text-dim" />
      </Link>
    );

  return (
    <div className="mx-4 mt-3 flex items-center gap-3 rounded-xl bg-raised p-4">
      <Icon name="sparkle" size={24} className="shrink-0 text-accent" />
      <div className="min-w-0 text-[13px] text-muted">
        <span className="font-semibold text-fg">«Микс дня»</span> появится в {settings.mix_hour}:00
      </div>
    </div>
  );
}

export function MixPage({ id }: { id: number }) {
  const { me } = useSession();
  // an unknown mix (an old link) is null: the page goes to today's
  const res = useAsync((s) => Promise.all([api.mix(id, s).catch((e) => (e instanceof ApiError && e.status === 404 ? null : Promise.reject(e))), api.mixToday(s)]), [id]);
  const [rebuilding, setRebuilding] = useState(false);
  const [modeBusy, setModeBusy] = useState(false);
  const mix: MixOut | null | undefined = res.data?.[0];
  const today: MixTodayOut | undefined = res.data?.[1];
  const settings = today?.settings;
  const building = rebuilding || !!today?.building;
  const current = !!today?.mix && today.mix.id === id;
  const pending = mix?.tracks.filter((t) => t.download && DOWNLOAD_ACTIVE.includes(t.download.status)).length ?? 0;

  useInterval(() => res.reload(true), mix && (building || pending || mix.ready < mix.total) ? (building ? BUILD_POLL_MS : POLL_MS) : null);
  useHubEvent('download', (d) => {
    res.setData((prev) => {
      const currentMix = prev?.[0];
      if (!currentMix?.tracks.some((t) => t.download?.id === d.id) || !prev) return prev;
      const tracks = currentMix.tracks.map((t) => t.download?.id === d.id ? { ...t, download: d } : t);
      return [{ ...currentMix, tracks }, prev[1]];
    });
  });
  useEffect(() => {
    if (res.data && !res.data[0] && today?.mix) navigate(`/mix/${today.mix.id}`, { replace: true });
  }, [res.data, today]);
  // a new mix replaced this one: go there
  useEffect(() => {
    if (!rebuilding || !today?.mix || today.mix.id === id) return;
    setRebuilding(false);
    toast('Новый микс готов', 'success');
    navigate(`/mix/${today.mix.id}`, { replace: true });
  }, [rebuilding, today, id]);

  if (res.loading && !res.data) return <SkeletonRows n={8} />;
  if (res.error && !res.data) return <ErrorState text={res.error} onRetry={() => res.reload()} />;
  if (res.data && !mix && !today?.mix) return <ErrorState text="Микс не найден" />;
  if (!mix || !settings) return null;

  const tracks = mix.tracks;
  const length = tracks.reduce((s, t) => s + (t.duration_s ?? 0), 0);
  const context = mixContext(mix.id);
  // Older apps: the library songs of the mix play in the app's player, in mix order.
  const oldApp = canPlay() && !canPlayAll();
  const playOld = (from?: MixTrackOut) =>
    playList(
      tracks.filter((t) => t.owned),
      (t) => t.nd_song_id,
      from,
    );
  const playAll =
    tracks.length === 0
      ? null
      : canPlayAll()
        ? () => playTracks(tracks, undefined, context, 'mix_page')
        : oldApp
          ? tracks.some((t) => t.owned && t.nd_song_id)
            ? () => playOld()
            : null
          : () => listen(listenTrack(tracks[0]), tracks.map(listenTrack), context, 'mix_page');

  const setMode = async (mode: MixMode) => {
    if (mode === settings.mode) return;
    setModeBusy(true);
    const st = await attempt(() => api.mixSettings({ mode }));
    setModeBusy(false);
    if (!st) return;
    res.setData((prev) => (prev ? [prev[0], { ...prev[1], settings: st }] : prev));
    toast('Режим применится к следующему миксу', 'success');
  };

  const rebuild = async () => {
    const r = await attempt(() => api.rebuildMix());
    if (!r) return;
    setRebuilding(true);
    toast(r.queued ? 'Собираем новый микс — пара минут' : 'Микс уже собирается', 'info');
    res.reload(true);
  };

  return (
    <div>
      <div className="relative">
        <div className="absolute inset-0 overflow-hidden" aria-hidden>
          <MixCover tracks={tracks} className="h-full w-full scale-125 rounded-none opacity-40 blur-3xl" />
          <div className="absolute inset-0 bg-gradient-to-b from-transparent via-bg/50 to-bg" />
        </div>
        <div className="pt-safe relative flex h-14 items-center justify-between px-2">
          <IconButton icon="back" label="Назад" onClick={() => goBack('/search')} className="bg-black/30 !text-white" />
          {current && me.is_admin && (
            <IconButton icon="refresh" label="Пересобрать микс" busy={building} onClick={rebuild} />
          )}
        </div>
        <div className="relative flex flex-col items-center px-6 pb-4 text-center">
          <MixCover tracks={tracks} large className="aspect-square w-[min(56vw,220px)] shadow-2xl" />
          <h1 className="mt-4 text-2xl font-extrabold leading-tight">{mix.title}</h1>
          <div className="mt-2 text-sm text-dim">{[songsWord(mix.total), length ? fmtLength(length) : null].filter(Boolean).join(' · ')}</div>
          {mix.ready < mix.total && (
            <div className="mt-3 w-full max-w-xs" role="status">
              <div className="mb-1 flex items-center justify-center gap-1.5 text-xs text-info">
                {pending > 0 && <Spinner size={12} />}
                {mix.ready} из {mix.total} в библиотеке
              </div>
              <ProgressBar value={mix.total ? mix.ready / mix.total : 0} />
            </div>
          )}
          {!current && <div className="mt-2 text-xs text-dim">Это прежний микс: сегодня уже собран новый</div>}
        </div>
      </div>

      <div className="px-4 pb-2">{playAll && <Button variant="primary" icon="play" block onClick={playAll}>Слушать всё</Button>}</div>

      {current && <MixModes settings={settings} busy={modeBusy} onMode={setMode} />}

      <section aria-label="Песни микса" className="pt-2">
        {tracks.map((t) => (
          <div key={`${t.position}:${t.ref}`}>
            <TrackRow track={t} link={false} onPlay={oldApp ? () => playOld(t) : undefined} queue={tracks} />
            {t.download && <MixTrackStatus download={t.download} />}
          </div>
        ))}
        {!tracks.length && <p className="px-4 py-6 text-center text-muted">В миксе пока нет песен</p>}
      </section>
    </div>
  );
}

function MixTrackStatus({ download }: { download: NonNullable<MixTrackOut['download']> }) {
  const text = [
    downloadStatusLabel(download.waiting_for_soulseek ? 'waiting_soulseek' : download.status, download.progress),
    download.provider && !download.waiting_for_soulseek ? providerLabel(download.provider) : null,
    download.audio_format?.toUpperCase(),
  ].filter(Boolean).join(' · ');
  return (
    <div className="mb-2 ml-[76px] mr-4 text-xs text-muted">
      {download.id ? <Link to={`/downloads/${download.id}`} className="hover:underline">{text}</Link> : text}
      {download.status === 'downloading' && <ProgressBar value={download.progress} className="mt-1 max-w-40" />}
    </div>
  );
}

function MixModes({
  settings,
  busy,
  onMode,
}: {
  settings: MixSettingsOut;
  busy: boolean;
  onMode: (m: MixMode) => void;
}) {
  return (
    <section aria-label="Режим микса" className="grid grid-cols-3 gap-2 px-4 pb-2 pt-2">
      {MODES.map(({ value, label, hint }) => (
        <button
          key={value}
          type="button"
          aria-pressed={settings.mode === value}
          disabled={busy}
          onClick={() => onMode(value)}
          className={cx('min-w-0 rounded-lg border px-1 py-2 text-center text-sm transition disabled:opacity-60',
            settings.mode === value ? 'border-accent/60 bg-accent/10 text-fg' : 'border-line bg-raised hover:bg-hover')}
        >
          <span className="block font-semibold">{label}</span>
          <span className="block text-xs text-muted">{hint}</span>
        </button>
      ))}
    </section>
  );
}

/** «Что вы слушаете?»: 3–5 artists for a user without history; the first pick builds the first mix at once. */
export function TastePage() {
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim(), 400);
  const res = useAsync((s) => api.tasteOnboarding(dq || undefined, s), [dq]);
  const [picked, setPicked] = useState<Map<string, string>>(new Map());
  const [busy, setBusy] = useState(false);
  const [seeded, setSeeded] = useState(false);

  // artists picked earlier come back checked
  useEffect(() => {
    if (seeded || !res.data) return;
    setSeeded(true);
    if (res.data.selected.length) setPicked(new Map(res.data.selected.map((a) => [a.ref, a.name])));
  }, [res.data, seeded]);

  const toggle = (a: CatalogArtist) =>
    setPicked((prev) => {
      const next = new Map(prev);
      if (next.has(a.ref)) next.delete(a.ref);
      else if (next.size >= PICK_MAX) toast(`Не больше ${PICK_MAX} исполнителей`, 'info');
      else next.set(a.ref, a.name);
      return next;
    });

  const submit = async () => {
    setBusy(true);
    const st = await attempt(() => api.pickArtists([...picked.keys()]));
    setBusy(false);
    if (!st) return;
    toast('Собираем ваш «Микс дня» — пара минут', 'success');
    navigate('/search', { replace: true });
  };

  const left = PICK_MIN - picked.size;
  return (
    <div>
      <PageHeader title="Что вы слушаете?" back="/search" />
      <p className="px-4 text-sm text-muted">
        Выберите {PICK_MIN}–{PICK_MAX} исполнителей — по ним соберём ваш первый «Микс дня». Дальше он будет учиться на том, что вы слушаете.
      </p>
      <div className="relative px-4 pt-3">
        <Icon name="search" size={20} className="pointer-events-none absolute left-7 top-1/2 mt-1.5 -translate-y-1/2 text-dim" />
        <input
          type="search"
          enterKeyHint="search"
          className="input h-12 rounded-full pl-11"
          placeholder="Найти исполнителя"
          aria-label="Найти исполнителя"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
        />
      </div>
      {picked.size > 0 && (
        <div className="no-scrollbar flex gap-2 overflow-x-auto px-4 pt-3">
          {[...picked].map(([ref, name]) => (
            <button
              key={ref}
              type="button"
              onClick={() => toggle({ ref, name, source: 'dz', id: ref.slice(3) })}
              className="inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-full bg-accent px-3.5 text-sm font-semibold text-on-accent"
              aria-label={`Убрать ${name}`}
            >
              {name}
              <Icon name="x" size={16} />
            </button>
          ))}
        </div>
      )}

      {res.loading && !res.data ? (
        <SkeletonRows n={6} />
      ) : res.error ? (
        <ErrorState text={res.error} onRetry={() => res.reload()} />
      ) : !res.data?.suggestions.length ? (
        <p className="px-4 py-8 text-center text-muted">Никого не нашли — попробуйте другое написание</p>
      ) : (
        <ul className="grid grid-cols-3 gap-x-2 gap-y-4 px-4 pb-4 pt-4">
          {res.data.suggestions.map((a) => {
            const on = picked.has(a.ref);
            return (
              <li key={a.ref}>
                <button type="button" onClick={() => toggle(a)} aria-pressed={on} className="flex w-full flex-col items-center gap-2 text-center">
                  <span className="relative">
                    <Cover src={a.picture_small_url || a.picture_url} size={88} round icon="user" alt={a.name} className={cx('transition', on && 'ring-4 ring-accent')} />
                    {on && (
                      <span className="absolute -bottom-1 -right-1 flex h-7 w-7 items-center justify-center rounded-full bg-accent text-on-accent ring-2 ring-bg">
                        <Icon name="check" size={16} stroke={3} />
                      </span>
                    )}
                  </span>
                  <span className={cx('line-clamp-2 text-sm leading-tight', on && 'font-semibold text-accent')}>{a.name}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <div className="sticky bottom-[calc(var(--tabbar-h)+var(--player-h,0px)+env(safe-area-inset-bottom))] z-10 bg-gradient-to-t from-bg via-bg to-transparent px-4 pb-3 pt-6">
        <Button variant="primary" block busy={busy} disabled={left > 0} onClick={submit}>
          {left > 0 ? `Выберите ещё ${left}` : 'Собрать микс'}
        </Button>
      </div>
    </div>
  );
}
