#!/usr/bin/env Rscript
# Refreshes the current AFL season from AFL Tables and rewrites the master CSV.
# Seasons before the current one are never touched — they are already final.
#
# Usage:  Rscript update_afl_data.R [path/to/afl_player_games_2015_2026.csv]

suppressPackageStartupMessages({
  library(fitzRoy)
  library(dplyr)
  library(readr)
})

args   <- commandArgs(trailingOnly = TRUE)
MASTER <- if (length(args) > 0) args[1] else "afl_player_games_2015_2026.csv"

# AFL seasons run Feb-Sep. In Oct-Dec, "current" is still this calendar year.
SEASON <- as.integer(format(Sys.Date(), "%Y"))

message("Fetching ", SEASON, " from AFL Tables. This takes a few minutes.")
raw <- fetch_player_stats_afltables(season = SEASON, rescrape = TRUE)

if (is.null(raw) || nrow(raw) == 0) {
  message("No rows returned for ", SEASON, ". Master file left unchanged.")
  quit(status = 0)
}
message("Fetched ", nrow(raw), " player-game rows.")

# Final score as goals.behinds, using extra-time totals when a match went past Q4.
score_gb <- function(g4, b4, gET, bET) {
  g <- ifelse(is.na(gET), g4, gET)
  b <- ifelse(is.na(bET), b4, bET)
  paste0(g, ".", b)
}

fresh <- raw %>%
  mutate(
    home     = Playing.for == Home.team,
    home_gb  = score_gb(HQ4G, HQ4B, HQETG, HQETB),
    away_gb  = score_gb(AQ4G, AQ4B, AQETG, AQETB),
    date     = as.character(Date),
    time     = sprintf("%02d:%02d",
                       as.integer(Local.start.time) %/% 100,
                       as.integer(Local.start.time) %%  100),
    venue    = Venue,
    team     = Playing.for,
    team_score     = ifelse(home, home_gb, away_gb),
    opponent       = ifelse(home, Away.team, Home.team),
    opponent_score = ifelse(home, away_gb, home_gb),
    year     = as.character(SEASON),
    round    = as.character(Round),
    player   = Player,
    disposals = Disposals, kicks = Kicks, handballs = Handballs, goals = Goals,
    time_on_ground_pct = Time.on.Ground
  ) %>%
  select(date, time, venue, team, team_score, opponent, opponent_score,
         year, round, player, disposals, kicks, handballs, goals,
         time_on_ground_pct)

# --- sanity checks before we overwrite anything ---
stopifnot(nrow(fresh) > 0, !any(is.na(fresh$date)), !any(is.na(fresh$team)))

if (file.exists(MASTER)) {
  master <- read_csv(MASTER, col_types = cols(.default = col_character()))
  prior  <- sum(master$year == as.character(SEASON))

  # Refuse to shrink the season — guards against a half-failed scrape.
  if (nrow(fresh) < prior) {
    stop("Fetched ", nrow(fresh), " rows but master already holds ", prior,
         " for ", SEASON, ". Refusing to overwrite. Master left unchanged.")
  }

  file.copy(MASTER, paste0(MASTER, ".bak"), overwrite = TRUE)
  history <- master %>% filter(year != as.character(SEASON))
  message("Replacing ", prior, " existing ", SEASON, " rows with ", nrow(fresh), ".")
} else {
  history <- fresh[0, ]
  message("No master file found. Writing ", SEASON, " only.")
}

out <- bind_rows(history, mutate(fresh, across(everything(), as.character))) %>%
  arrange(date, venue, team, player)

write_csv(out, MASTER, na = "")
message("Wrote ", nrow(out), " rows to ", MASTER)
message("Seasons: ", paste(sort(unique(out$year)), collapse = ", "))
