import { api } from '../api/client';
import { Collage } from '../components/Charts';
import { TrackRow, listenTrack } from '../components/catalog';
import { Button, ErrorState, IconButton, Loading } from '../components/ui';
import { tracksWord } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { listenAll, shuffled } from '../lib/listen';
import { canPlayAll, playTracks } from '../lib/player';
import { goBack } from '../lib/router';
import { canWave, seedKey, startWave, useWave } from '../lib/wave';

/** A chart or genre (`components/Charts.tsx`): its tracks by their places, played in this order, shuffled or as a
 * wave (the Hub's «Моя волна» from the chart: its top places first, then what sounds like them and like the user). The
 * first request of a chart that was never made may take a minute: the Hub matches its names with the catalog. */
export function ChartPage({ id }: { id: string }) {
  const res = useAsync((s) => api.chart(id, s), [id]);
  const { starting } = useWave();

  if (res.loading && !res.data)
    return (
      <>
        <Bar />
        <Loading text="Собираю чарт… В первый раз это может занять до минуты" />
      </>
    );
  if (res.error || !res.data)
    return (
      <>
        <Bar />
        <ErrorState text={res.error ?? 'Чарт не найден'} onRetry={() => res.reload()} />
      </>
    );

  const { chart, tracks } = res.data;
  const context = `chart:${chart.id}`;
  const play = (shuffle: boolean) => {
    if (!tracks.length) return;
    // the app's player (1.2.0+) plays the whole list; a browser — the page's own
    if (canPlayAll()) playTracks(shuffle ? shuffled(tracks) : tracks, undefined, context, 'chart_page');
    else listenAll(tracks.map(listenTrack), { shuffle, context, origin: 'chart_page' });
  };
  const seed = { chart: chart.id };

  return (
    <div>
      <Bar />
      <div className="flex flex-col items-center px-6 pb-4 text-center">
        <Collage chart={chart} className="w-[min(56vw,220px)] shadow-2xl" />
        <h1 className="mt-4 text-2xl font-extrabold leading-tight">{chart.name}</h1>
        <div className="mt-1 text-sm text-muted">
          {[chart.source, tracks.length ? tracksWord(tracks.length) : null, updated(chart.updated_at)].filter(Boolean).join(' · ')}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 px-4 pb-2">
        <Button variant="primary" icon="play" disabled={!tracks.length} onClick={() => play(false)}>
          По порядку
        </Button>
        <Button icon="shuffle" disabled={!tracks.length} onClick={() => play(true)}>
          Вперемешку
        </Button>
        {canWave() && (
          <Button icon="wave" className="col-span-2" disabled={!tracks.length} busy={starting === seedKey(seed)}
            onClick={() => void startWave(seed, 'chart_page')}>
            Волна по чарту
          </Button>
        )}
      </div>

      <section aria-label="Треки" className="pt-2">
        {tracks.map((t) => (
          <TrackRow key={t.ref} track={t} number={t.position} queue={tracks} />
        ))}
        {!tracks.length && <p className="px-4 py-6 text-center text-muted">В чарте нет треков, которые можно послушать</p>}
      </section>
    </div>
  );
}

function updated(at?: string | null): string | null {
  if (!at) return null;
  const d = new Date(at);
  const today = new Date().toDateString() === d.toDateString();
  const time = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  return today ? `обновлён в ${time}` : `обновлён ${d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' })}`;
}

function Bar() {
  return (
    <div className="pt-safe sticky top-0 z-20 flex h-14 items-center bg-bg/90 px-2">
      <IconButton icon="back" label="Назад" onClick={() => goBack('/search')} />
    </div>
  );
}
