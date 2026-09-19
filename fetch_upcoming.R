#!/usr/bin/env Rscript
# Writes upcoming.csv for predict.py: the next round's fixture (AFL source) and
# whatever lineups have been named, one row per named player.
#
#   - A team with no lineup yet gets a single row with a blank player and
#     named = FALSE. predict.py then falls back to that team's last 22 from the
#     master CSV and flags "team not yet named".
#   - date / time are LOCAL to the venue, matching the master CSV.
#   - Any failure at all writes a header-only file and exits 0, so this step can
#     never break the workflow. predict.py treats that as "no upcoming games".
#
# Usage: Rscript fetch_upcoming.R [upcoming.csv]

OUT  <- { a <- commandArgs(trailingOnly = TRUE); if (length(a) >= 1) a[1] else "upcoming.csv" }
COLS <- c("season", "round", "round_name", "date", "time", "utc_start", "venue", "venue_tz",
          "home_team", "away_team", "team", "player", "given", "surname", "position", "named")

write_out <- function(df) {
  if (is.null(df) || nrow(df) == 0) {
    df <- as.data.frame(setNames(replicate(length(COLS), character(0), simplify = FALSE), COLS))
  }
  write.csv(df[, COLS, drop = FALSE], OUT, row.names = FALSE, na = "")
}

# First of several possible column names - the AFL API's shape shifts now and then.
pick <- function(df, candidates, default = NA_character_) {
  for (cn in candidates) if (cn %in% names(df)) return(as.character(df[[cn]]))
  rep(default, nrow(df))
}

parse_utc <- function(x) as.POSIXct(substr(x, 1, 19), format = "%Y-%m-%dT%H:%M:%S", tz = "UTC")

main <- function() {
  suppressPackageStartupMessages(library(fitzRoy))
  now    <- Sys.time()
  season <- as.integer(format(now, "%Y", tz = "Australia/Melbourne"))

  fx <- NULL
  for (s in c(season, season + 1L)) {          # after the grand final, look to next year
    raw <- tryCatch(fetch_fixture(season = s, comp = "AFLM", source = "AFL"),
                    error = function(e) { message("fixture ", s, ": ", conditionMessage(e)); NULL })
    if (is.null(raw) || nrow(raw) == 0) next
    raw <- as.data.frame(raw)
    f <- data.frame(
      season    = s,
      round     = suppressWarnings(as.integer(pick(raw, c("round.roundNumber", "roundNumber", "round")))),
      round_name = pick(raw, c("round.name", "roundName")),
      utc_start = pick(raw, c("utcStartTime", "utc_start_time")),
      status    = toupper(pick(raw, c("status"), "SCHEDULED")),
      venue     = pick(raw, c("venue.name", "venueName", "venue")),
      venue_tz  = pick(raw, c("venue.timezone", "venueTimezone"), "Australia/Melbourne"),
      home_team = pick(raw, c("home.team.name", "homeTeam.name", "home_team")),
      away_team = pick(raw, c("away.team.name", "awayTeam.name", "away_team")),
      stringsAsFactors = FALSE)
    f$start <- parse_utc(f$utc_start)
    f <- f[!is.na(f$start) & f$start > now & !is.na(f$round) &
           !(f$status %in% c("CONCLUDED", "COMPLETED", "POSTGAME", "CANCELLED")) &
           !is.na(f$home_team) & !is.na(f$away_team) & nzchar(f$home_team) & nzchar(f$away_team), ]
    if (nrow(f) == 0) next
    f <- f[f$round == f$round[which.min(f$start)], ]     # the next round still to be played
    fx <- f[order(f$start), ]
    break
  }
  if (is.null(fx)) { message("No upcoming fixtures."); return(write_out(NULL)) }

  fx$venue_tz[is.na(fx$venue_tz) | !(fx$venue_tz %in% OlsonNames())] <- "Australia/Melbourne"
  # mapply drops the POSIXct class, so format from the numeric value explicitly
  fx$date <- mapply(function(t, tz) format(as.POSIXct(t, origin = "1970-01-01", tz = "UTC"), "%Y-%m-%d", tz = tz),
                    as.numeric(fx$start), fx$venue_tz)
  fx$time <- mapply(function(t, tz) format(as.POSIXct(t, origin = "1970-01-01", tz = "UTC"), "%H:%M", tz = tz),
                    as.numeric(fx$start), fx$venue_tz)

  lu <- tryCatch(fetch_lineup(season = fx$season[1], round_number = fx$round[1], comp = "AFLM"),
                 error = function(e) { message("lineup: ", conditionMessage(e)); NULL })
  if (!is.null(lu) && nrow(lu) > 0) {
    lu <- as.data.frame(lu)
    lu <- data.frame(
      team     = pick(lu, c("teamName", "team.name", "team")),
      given    = pick(lu, c("player.playerName.givenName", "givenName", "player.givenName")),
      surname  = pick(lu, c("player.playerName.surname", "surname", "player.surname")),
      position = pick(lu, c("position", "player.position"), ""),
      stringsAsFactors = FALSE)
    lu <- lu[!is.na(lu$team) & !is.na(lu$surname) & nzchar(lu$surname), ]
  } else lu <- NULL

  rows <- list()
  for (i in seq_len(nrow(fx))) {
    g <- fx[i, c("season", "round", "round_name", "date", "time", "utc_start", "venue", "venue_tz",
                 "home_team", "away_team")]
    for (tm in c(fx$home_team[i], fx$away_team[i])) {
      p <- if (is.null(lu)) NULL else lu[lu$team == tm, ]
      if (is.null(p) || nrow(p) == 0) {
        rows[[length(rows) + 1]] <- cbind(g, team = tm, player = "", given = "", surname = "",
                                          position = "", named = FALSE, row.names = NULL)
      } else {
        rows[[length(rows) + 1]] <- cbind(g[rep(1, nrow(p)), ], team = tm,
          player = trimws(paste(p$given, p$surname)), given = p$given, surname = p$surname,
          position = p$position, named = TRUE, row.names = NULL)
      }
    }
  }
  out <- do.call(rbind, rows)
  write_out(out)
  message(sprintf("%s: %d games, %d named players -> %s", fx$round_name[1], nrow(fx),
                  sum(out$named), OUT))
}

ok <- tryCatch({ main(); TRUE }, error = function(e) { message("fetch_upcoming failed: ", conditionMessage(e)); FALSE })
if (!ok) try(write_out(NULL), silent = TRUE)
quit(status = 0)
