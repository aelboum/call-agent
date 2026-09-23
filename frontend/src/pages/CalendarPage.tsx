/** Calendar/agenda view (Phase 2.15 brief §11), built on the new
 * `GET /v1/calendar-events` list endpoint (Phase 2.15 backend addition --
 * see `README.md`). A bounded date range, never an unbounded full-calendar
 * fetch. */
import { useState } from "react";

import { QueryState } from "../components/states/States";
import { useCalendarEvents, useCalendars, useCancelCalendarEvent } from "../lib/hooks";

function toDateInputValue(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function CalendarPage() {
  const [rangeStart, setRangeStart] = useState(() => toDateInputValue(new Date()));
  const [rangeDays, setRangeDays] = useState(14);
  const [calendarId, setCalendarId] = useState<string>("");

  const calendars = useCalendars();
  const startAt = new Date(`${rangeStart}T00:00:00Z`).toISOString();
  const endAt = new Date(
    new Date(`${rangeStart}T00:00:00Z`).getTime() + rangeDays * 24 * 60 * 60 * 1000,
  ).toISOString();
  const events = useCalendarEvents(startAt, endAt, calendarId || undefined);
  const cancelEvent = useCancelCalendarEvent();

  return (
    <div className="page calendar-page">
      <h1>Calendar</h1>
      <div className="calendar-controls">
        <label>
          From
          <input
            type="date"
            value={rangeStart}
            onChange={(event) => setRangeStart(event.target.value)}
          />
        </label>
        <label>
          Days
          <input
            type="number"
            min={1}
            max={90}
            value={rangeDays}
            onChange={(event) => setRangeDays(Number(event.target.value) || 14)}
          />
        </label>
        <label>
          Calendar
          <select value={calendarId} onChange={(event) => setCalendarId(event.target.value)}>
            <option value="">All calendars</option>
            <QueryState query={calendars}>
              {(rows) => (
                <>
                  {rows.map((calendar) => (
                    <option key={calendar.id} value={calendar.id}>
                      {calendar.name}
                    </option>
                  ))}
                </>
              )}
            </QueryState>
          </select>
        </label>
      </div>
      <QueryState query={events} emptyMessage="Nothing scheduled in this range.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>Title</th>
                <th>Start</th>
                <th>End</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((event) => (
                <tr key={event.id}>
                  <td>{event.title}</td>
                  <td>{new Date(event.start_at).toLocaleString()}</td>
                  <td>{new Date(event.end_at).toLocaleString()}</td>
                  <td>
                    <span className={`badge badge-${event.status}`}>{event.status}</span>
                  </td>
                  <td>
                    {event.status === "scheduled" && (
                      <button
                        type="button"
                        disabled={cancelEvent.isPending}
                        onClick={() => cancelEvent.mutate({ eventId: event.id })}
                      >
                        Cancel
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryState>
    </div>
  );
}
