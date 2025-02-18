import logging
import math
import ngram
import re

from rtree import index
from haversine import haversine, Unit

from osm_stop_matcher.util import  drop_table_if_exists, backup_table_if_exists

class StopMatcher():
	UNKNOWN_MODE_RATING = 0.3
	official_matches = {}
	osm_matches = {}
	errors = {}

	def __init__(self, db):
		self.db = db
		self.osm_stops = index.Index()
		self.logger = logging.getLogger('osm_stop_matcher.StopMatcher')	

	def match_stops(self):
		self._match_stops('%', export = True)

	def _match_stops(self, id_pattern = '%', export = False):
		self.logger.info("Loading osm data to index")
		self.load_osm_index()
		self.logger.info("Loaded osm data to index")
		row = 0
		cur = self.db.execute("SELECT * FROM haltestellen_unified where lon IS NOT NULL AND globaleID like ?", [id_pattern])
		stops = cur.fetchall()
		for stop in stops:
			row += 1
			self.match_stop(stop, stop["globaleID"], (float(stop["lat"]),float(stop["lon"])), row)
		
		self.logger.info("Matched stops")
		if export:	
			self.export_match_candidates()
			self.logger.info("Exported candidates")

	def load_osm_index(self):
		cur = self.db.execute("SELECT * FROM osm_stops")
		cnt = 0
		rows = cur.fetchall()
		for stop in rows:
			cnt += 1
			lat = stop["lat"]
			lon = stop["lon"]
			id = stop["osm_id"]
			stop = {
				"id": id,
				"name": stop["name"],
				"network": stop["network"],
				"operator": stop["operator"],
				"lat": lat,
				"lon": lon,
				"mode": stop["mode"],
				"type": stop["type"],
				"ref": stop["ref"],
				"ref_key": stop["ref_key"],
				"next_stops": stop["next_stops"],
				"prev_stops": stop["prev_stops"],
				"assumed_platform": stop["assumed_platform"]
			}
			self.osm_stops.insert(id = cnt, coordinates=(lat, lon, lat, lon), obj= stop)

	def rank_successor_matching(self, stop, osm_stop):
		richtung = stop["Name_Steig"]
		ortsteil = stop['Ortsteil']
		gemeinde = stop['Gemeinde']

		if richtung:
			match = re.match('(.*)(eRtg|Ri |>|Ri\.|Rtg|Richt |Fahrtrichtung|Ri-|Ri:|Richtung|Richtg\.|FR )(.*)', richtung)
			if match:
				richtung = match.group(3).strip()
				richtung = richtung.replace(ortsteil, '').replace(gemeinde, '')
				richtung = richtung.replace(',', ' ')
			
				next_stops = osm_stop["next_stops"].replace(ortsteil, '').replace(gemeinde, '')  if osm_stop["next_stops"] else None
				prev_stops = osm_stop["prev_stops"].replace(ortsteil, '').replace(gemeinde, '') if osm_stop["prev_stops"] else None
				
				similarity_next = ngram.NGram.compare(richtung, next_stops,N=1)
				similarity_prev = ngram.NGram.compare(richtung, prev_stops,N=1)
				if similarity_next > 0.7 and similarity_prev < 0.6:
					return 1
				elif similarity_prev > 0.7 and similarity_next < 0.6:
					return -1
		return 0

	def rank_mode(self, stop, candidate):
		if (candidate["mode"] == 'bus' and stop["mode"] == 'bus' or 
			candidate["mode"] == 'light_rail' and stop["mode"] == 'light_rail' or
			candidate["mode"] == 'train' and stop["mode"] == 'train' or
			candidate["mode"] == 'trainish' and stop["mode"] in ['train', 'light_rail'] or
			candidate["mode"] == 'tram' and stop["mode"] == 'tram'):
			return 1
		elif not candidate["mode"] or not stop["mode"]:
			return 0.7
		else:
			return 0

	def rank_candidate(self, stop, candidate, distance):
		osm_name = candidate["name"]
		name_distance_short_name = ngram.NGram.compare(stop["Haltestelle"],osm_name,N=1)
		name_distance_long_name = ngram.NGram.compare(stop["Haltestelle_lang"],osm_name,N=1)
		if (stop["Haltestelle"] == '' or stop["Haltestelle"] == None) and (stop["Haltestelle_lang"] == '' or stop["Haltestelle_lang"] == None):
			self.logger.info("Stop %s has no name. Use fix name_distance", stop["globaleID"])
			name_distance_short_name = 0.3
		elif osm_name == '' or osm_name == None:
			self.logger.info("OSM stop %s has no name. Use fix name_distance", candidate["id"])
			name_distance_short_name = 0.3
			name_distance_long_name = 0.3
		
		(short_name_matched, matched_name) = (False, stop["Haltestelle_lang"]) if name_distance_short_name < name_distance_long_name else (True, stop["Haltestelle"])
		name_distance = max(name_distance_short_name, name_distance_long_name)
		platform_id = stop["globaleID"]
		ifopt_platform = platform_id[platform_id.rfind(":") + 1 :] if platform_id and platform_id.count(':') > 3 else None
		platform_matches = ifopt_platform == str(candidate["assumed_platform"])
		platform_mismatches = not ifopt_platform == None and not candidate["assumed_platform"] == None and not platform_matches
		mode_rating = self.rank_mode(stop, candidate)
		successor_rating = self.rank_successor_matching(stop, candidate)
		
		if candidate["ref"] == stop["globaleID"]:
			# TODO: We currently ignore, that OSM IFOPTS are currently duplicated for some stops...
			rating = 1
		else:
			rating = name_distance / ( 1 + distance )
			# We boost a candidate if steig matches
			if platform_mismatches:
				# Note: since OSM has some refs wrongly tagged as bus route number...
				rating = rating*0.5

			rating = rating ** (1 - successor_rating * 0.2 - mode_rating * 0.1 - mode_rating * platform_matches * 0.5)

		self.logger.debug("rating: %s name_distance: %s matched_name: %s osm_name: %s platform_matches: %s successor_rating: %s, mode_rating: %s", rating, name_distance, matched_name, osm_name, platform_matches, successor_rating, mode_rating)
		return (rating, name_distance, matched_name, osm_name, platform_matches, successor_rating, mode_rating)

	def rank_candidates(self, stop, stop_id, coords, candidates):
		matches = []
		last_name_distance = 0
		for candidate in candidates:
			self.logger.debug('rank %s', candidate)
			# estimate distance
			distance = haversine(coords, (candidate["lat"],candidate["lon"]), unit=Unit.METERS)
			if distance > 400:
				return matches
		   
			# Ignore bahn candidates when looking for bus_stop
			if candidate["mode"] in ['trainish', 'train','light_rail','tram'] and "bus" == stop["mode"]:
				continue
			# Ignore bus candidates when looking for railway stops
			if candidate["mode"] == 'bus' and stop["mode"] in ["tram", "light_rail", "train"]:
				continue
			
			(rating, name_distance, matched_name, osm_name, platform_matches, successor_rating, mode_rating) = self.rank_candidate(stop, candidate, distance)
			#if last_name_distance > name_distance:
			if last_name_distance > name_distance and name_distance < 0.3:
				self.logger.info("Ignore {} ({})  {} ({}) with distance {} and name similarity {}. Platform matches? {} as name distance low".format(matched_name,stop_id, osm_name, candidate["id"], distance, name_distance,platform_matches))
				continue
			elif rating < 0.001:
				self.logger.info("Ignore {} ({})  {} ({}) as rating {} is low".format(matched_name,stop_id, osm_name, candidate["id"], distance, name_distance,platform_matches))
				continue
			self.logger.info("{} ({}) might match {} ({}) with distance {} and name similarity {}. Platform matches? {}".format(matched_name,stop_id, osm_name, candidate["id"], distance, name_distance,platform_matches))
			
			matches.append({"globalID": stop_id, "match": candidate, "name_distance": name_distance, "distance": distance, "platform_matches": platform_matches, "successor_rating": successor_rating, "rating": rating, "mode_rating": mode_rating})
			last_name_distance = name_distance
		return matches

	def store_matches(self, stop, offical_stop_id, matches):
		self.official_matches[offical_stop_id] = matches
		
		for match in matches:
			osm_id = match["match"]["id"]
			if not osm_id in self.osm_matches:
				 self.osm_matches[osm_id] = []
			self.osm_matches[osm_id].append(match)

	def is_bus_station(self, stop):
		name = stop["Haltestelle"]  if stop["Haltestelle"] else stop["Haltestelle_lang"] 
		return name and ('ahnhof' in name
			or 'ZOB' in name
			or 'Schulzentrum' in name
			or 'Flughafen' in name
			or ' Bf' in name )

	def match_stop(self, stop, stop_id, coords, row):
		no_of_candidates = 15 if self.is_bus_station(stop) else 10

		candidates = list(self.osm_stops.nearest(coords, no_of_candidates, objects='raw'))
		matches = self.rank_candidates(stop, stop_id, coords, candidates)
		if matches:	
			self.store_matches(stop, stop_id, matches)
	
	def export_match_candidates(self):
		drop_table_if_exists(self.db, "candidates")
		self.db.execute('''CREATE TABLE candidates
			 (ifopt_id text, osm_id text, rating real, distance real, name_distance real, platform_matches integer, successor_rating INTEGER, mode_rating real)''')
		for stop_id in self.official_matches:
			matches = self.official_matches[stop_id]
			rows = []
			for match in matches:
				rows.append((
					match["globalID"], 
					match["match"]["id"], 
					match["rating"], 
					match['distance'], 
					match['name_distance'], 
					match['platform_matches'],
					match['successor_rating'],
					match['mode_rating'],
					))
			self.logger.debug("export match candidates ", rows)
			self.db.executemany('INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?)', rows)
		self.db.commit()
		self.db.execute('''CREATE INDEX osm_index ON candidates(osm_id, rating DESC)''')
		self.db.execute('''CREATE INDEX ifopt_index ON candidates(ifopt_id, rating DESC)''')
		
		backup_table_if_exists(self.db, "matches", "matches_backup")

		drop_table_if_exists(self.db, "matches")
		self.db.execute("""CREATE TABLE matches AS
					SELECT * FROM candidates WHERE ifopt_id='Non existant'""")

		# Add Spatial columns
		try:
			self.db.execute("SELECT InitSpatialMetaData()")
			self.db.execute("SELECT AddGeometryColumn('osm_stops', 'the_geom', 4326, 'POINT','XY')")
			self.db.execute("SELECT AddGeometryColumn('matches', 'the_geom', 4326, 'LINESTRING','XY')")
			self.db.execute("SELECT AddGeometryColumn('candidates', 'the_geom', 4326, 'LINESTRING','XY')")
		except:
			pass
		self.db.execute("UPDATE osm_stops SET the_geom = MakePoint(lon,lat, 4326)")
		self.db.execute("""UPDATE matches SET the_geom = (
			SELECT LineFromText('LINESTRING('||o.lon||' '||o.lat||', '||n.lon||' '||n.lat||')', 4326) 
			  FROM osm_stops o, haltestellen_unified n  
			 WHERE o.osm_id = matches.osm_id AND matches.ifopt_id = n.globaleID AND n.lat IS NOT NULL)""") 
		self.db.execute("""UPDATE candidates SET the_geom = (
			SELECT LineFromText('LINESTRING('||o.lon||' '||o.lat||', '||n.lon||' '||n.lat||')', 4326) 
			  FROM osm_stops o, haltestellen_unified n  
			 WHERE o.osm_id = candidates.osm_id AND candidates.ifopt_id = n.globaleID AND n.lat IS NOT NULL)""")
		self.db.commit()
