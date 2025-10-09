# 25.07.25

import re
from urllib.parse import urljoin
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional, Tuple, Any


# External library
from curl_cffi import requests
from rich.console import Console


# Internal utilities
from StreamingCommunity.Util.config_json import config_manager


# Variable
console = Console()
max_timeout = config_manager.get_int('REQUESTS', 'timeout')
max_retry = config_manager.get_int('REQUESTS', 'max_retry')

class URLBuilder:

    @staticmethod
    def build_url(base: str, template: str, rep_id: Optional[str] = None, number: Optional[int] = None, time: Optional[int] = None, bandwidth: Optional[int] = None) -> str:
        """Build absolute URL preserving query/hash"""
        if not template:
            return None

        # Substitute RepresentationID and Bandwidth first
        if rep_id is not None:
            template = template.replace('$RepresentationID$', rep_id)
        if bandwidth is not None:
            template = template.replace('$Bandwidth$', str(bandwidth))

        # Handle $Number$ with optional formatting
        template = URLBuilder._replace_number(template, number)
        
        # Replace $Time$ if present
        if '$Time$' in template and time is not None:
            template = template.replace('$Time$', str(time))

        return URLBuilder._finalize_url(base, template)

    @staticmethod
    def _replace_number(template: str, number: Optional[int]) -> str:
        """Handle $Number$ placeholder with formatting"""
        def _replace_number_match(match):
            num = number if number is not None else 0
            fmt = match.group(1)

            if fmt:
                # fmt like %05d -> convert to python format
                m = re.match(r'%0(\d+)d', fmt)
                if m:
                    width = int(m.group(1))
                    return str(num).zfill(width)
                
            return str(num)

        return re.sub(r'\$Number(\%0\d+d)?\$', _replace_number_match, template)

    @staticmethod
    def _finalize_url(base: str, template: str) -> str:
        """Finalize URL construction preserving query and fragment"""

        # Split path/query/fragment to avoid urljoin mangling query
        split = template.split('#', 1)
        path_and_query = split[0]
        frag = ('#' + split[1]) if len(split) == 2 else ''
        
        if '?' in path_and_query:
            path_part, query_part = path_and_query.split('?', 1)
            abs_path = urljoin(base, path_part)

            # ensure we don't accidentally lose existing query separators
            final = abs_path + '?' + query_part + frag

        else:
            abs_path = urljoin(base, path_and_query)
            final = abs_path + frag

        return final


class SegmentTimelineParser:
    """Parser for SegmentTimeline elements"""
    
    def __init__(self, namespace: Dict[str, str]):
        self.ns = namespace

    def parse(self, seg_timeline_element) -> Tuple[List[int], List[int]]:
        """
        Parse SegmentTimeline and return (number_list, time_list)
        """
        number_list = []
        time_list = []
        
        if seg_timeline_element is None:
            return number_list, time_list

        current_time = None
        start_number = 1  # Default start number
        
        for s_element in seg_timeline_element.findall('mpd:S', self.ns):
            d = s_element.get('d')
            if d is None:
                continue
                
            d = int(d)
            r = int(s_element.get('r', 0))

            # Handle 't' attribute
            if s_element.get('t') is not None:
                current_time = int(s_element.get('t'))
            elif current_time is None:
                current_time = 0

            # Append (r+1) times and numbers
            for i in range(r + 1):
                number_list.append(start_number)
                time_list.append(current_time)
                start_number += 1
                current_time += d
                
        return number_list, time_list


class RepresentationParser:
    """Parser for individual representations"""
    
    def __init__(self, mpd_url: str, namespace: Dict[str, str]):
        self.mpd_url = mpd_url
        self.ns = namespace
        self.timeline_parser = SegmentTimelineParser(namespace)

    def parse_adaptation_set(self, adapt_set, base_url: str) -> List[Dict[str, Any]]:
        """
        Parse all representations in an adaptation set
        """
        representations = []
        mime_type = adapt_set.get('mimeType', '')
        lang = adapt_set.get('lang', '')
        
        # Find SegmentTemplate at AdaptationSet level
        adapt_seg_template = adapt_set.find('mpd:SegmentTemplate', self.ns)

        for rep_element in adapt_set.findall('mpd:Representation', self.ns):
            representation = self._parse_representation(rep_element, adapt_set, adapt_seg_template, base_url, mime_type, lang)
            if representation:
                representations.append(representation)
                
        return representations

    def _parse_representation(self, rep_element, adapt_set, adapt_seg_template, base_url: str, mime_type: str, lang: str) -> Optional[Dict[str, Any]]:
        """Parse a single representation"""
        rep_id = rep_element.get('id')
        bandwidth = rep_element.get('bandwidth')
        codecs = rep_element.get('codecs')
        width = rep_element.get('width')
        height = rep_element.get('height')

        # Try to find SegmentTemplate at Representation level
        rep_seg_template = rep_element.find('mpd:SegmentTemplate', self.ns)
        seg_tmpl = rep_seg_template if rep_seg_template is not None else adapt_seg_template
        
        if seg_tmpl is None:
            return None

        # Build URLs
        rep_base_url = self._resolve_base_url(rep_element, adapt_set, base_url)
        init_url, media_urls = self._build_segment_urls(seg_tmpl, rep_id, bandwidth, rep_base_url)

        return {
            'id': rep_id,
            'type': (mime_type.split('/')[0]) if mime_type else 'unknown',
            'codec': codecs,
            'bandwidth': int(bandwidth) if bandwidth else 0,
            'width': int(width) if width else 0,
            'height': int(height) if height else 0,
            'language': lang,
            'init_url': init_url,
            'segment_urls': media_urls
        }

    def _resolve_base_url(self, rep_element, adapt_set, initial_base: str) -> str:
        """Resolve base URL by concatenating MPD -> Period/AdaptationSet -> Representation BaseURLs"""
        base = initial_base

        # Adaptation-level BaseURL
        if adapt_set is not None:
            adapt_base = adapt_set.find('mpd:BaseURL', self.ns)
            if adapt_base is not None and adapt_base.text:
                base = urljoin(base, adapt_base.text.strip())

        # Representation-level BaseURL
        if rep_element is not None:
            rep_base = rep_element.find('mpd:BaseURL', self.ns)
            if rep_base is not None and rep_base.text:
                base = urljoin(base, rep_base.text.strip())

        return base

    def _build_segment_urls(self, seg_tmpl, rep_id: str, bandwidth: str, base_url: str) -> Tuple[str, List[str]]:
        """Build initialization and media segment URLs"""
        init = seg_tmpl.get('initialization')
        media = seg_tmpl.get('media')
        start_number = int(seg_tmpl.get('startNumber', 1))

        # Build init URL
        init_url = URLBuilder.build_url(
            base_url, init, 
            rep_id=rep_id, 
            bandwidth=int(bandwidth) if bandwidth else None
        ) if init else None

        # Parse segment timeline
        seg_timeline = seg_tmpl.find('mpd:SegmentTimeline', self.ns)
        number_list, time_list = self.timeline_parser.parse(seg_timeline)
        
        if not number_list:
            number_list = list(range(start_number, start_number + 100))

        # Build media URLs
        media_urls = self._build_media_urls(media, base_url, rep_id, bandwidth, number_list, time_list)

        return init_url, media_urls

    def _build_media_urls(self, media_template: str, base_url: str, rep_id: str, bandwidth: str, number_list: List[int], time_list: List[int]) -> List[str]:
        """Build list of media segment URLs"""
        if not media_template:
            return []

        media_urls = []
        bandwidth_int = int(bandwidth) if bandwidth else None

        if '$Time$' in media_template and time_list:
            for t in time_list:
                media_urls.append(URLBuilder.build_url(
                    base_url, media_template, 
                    rep_id=rep_id, time=t, bandwidth=bandwidth_int
                ))
        elif '$Number' in media_template and number_list:
            for n in number_list:
                media_urls.append(URLBuilder.build_url(
                    base_url, media_template, 
                    rep_id=rep_id, number=n, bandwidth=bandwidth_int
                ))
        else:
            media_urls.append(URLBuilder.build_url(
                base_url, media_template, 
                rep_id=rep_id, bandwidth=bandwidth_int
            ))

        return media_urls


class MPDParser:
    @staticmethod
    def get_best(representations):
        """
        Returns the video representation with the highest resolution/bandwidth, or audio with highest bandwidth.
        """
        videos = [r for r in representations if r['type'] == 'video']
        audios = [r for r in representations if r['type'] == 'audio']
        if videos:
            return max(videos, key=lambda r: (r['height'], r['width'], r['bandwidth']))
        elif audios:
            return max(audios, key=lambda r: r['bandwidth'])
        return None

    @staticmethod
    def get_worst(representations):
        """
        Returns the video representation with the lowest resolution/bandwidth, or audio with lowest bandwidth.
        """
        videos = [r for r in representations if r['type'] == 'video']
        audios = [r for r in representations if r['type'] == 'audio']
        if videos:
            return min(videos, key=lambda r: (r['height'], r['width'], r['bandwidth']))
        elif audios:
            return min(audios, key=lambda r: r['bandwidth'])
        return None

    @staticmethod
    def get_list(representations, type_filter=None):
        """
        Returns the list of representations filtered by type ('video', 'audio', etc.).
        """
        if type_filter:
            return [r for r in representations if r['type'] == type_filter]
        return representations

    def __init__(self, mpd_url: str):
        self.mpd_url = mpd_url
        self.pssh = None
        self.representations = []
        self.ns = {}
        self.root = None

    def parse(self, custom_headers: Dict[str, str]) -> None:
        """Parse the MPD file and extract all representations"""
        self._fetch_and_parse_mpd(custom_headers)
        self._extract_namespace()
        self._extract_pssh()
        self._parse_representations()

    def _fetch_and_parse_mpd(self, custom_headers: Dict[str, str]) -> None:
        """Fetch MPD content and parse XML"""
        for attempt in range(max_retry + 1):
            try:
                response = requests.get(
                    self.mpd_url, headers=custom_headers, timeout=max_timeout, 
                    allow_redirects=True, impersonate="chrome124"
                )
                
                response.raise_for_status()
                self.root = ET.fromstring(response.content)
                break

            except Exception as e:
                if attempt == max_retry:
                    raise e
                
                console.print(f"[bold yellow]Retrying... ({attempt + 1}/{max_retry})[/bold yellow]")

    def _extract_namespace(self) -> None:
        """Extract and register namespaces from the root element"""
        if self.root.tag.startswith('{'):
            uri = self.root.tag[1:].split('}')[0]
            self.ns['mpd'] = uri
            self.ns['cenc'] = 'urn:mpeg:cenc:2013'

    def _extract_pssh(self) -> None:
        """Extract PSSH from ContentProtection elements"""
        for protection in self.root.findall('.//mpd:ContentProtection', self.ns):
            pssh_element = protection.find('cenc:pssh', self.ns)
            if pssh_element is not None and pssh_element.text:
                self.pssh = pssh_element.text
                break

        if not self.pssh:
            console.print("[bold red]PSSH not found in MPD![/bold red]")

    def _parse_representations(self) -> None:
        """Parse all representations from the MPD"""
        base_url = self._get_initial_base_url()
        representation_parser = RepresentationParser(self.mpd_url, self.ns)

        for adapt_set in self.root.findall('.//mpd:AdaptationSet', self.ns):
            representations = representation_parser.parse_adaptation_set(adapt_set, base_url)
            self.representations.extend(representations)

    def _get_initial_base_url(self) -> str:
        """Get the initial base URL from MPD-level BaseURL"""
        base_url = self.mpd_url.rsplit('/', 1)[0] + '/'
        
        # MPD-level BaseURL
        mpd_base = self.root.find('mpd:BaseURL', self.ns)
        if mpd_base is not None and mpd_base.text:
            base_url = urljoin(base_url, mpd_base.text.strip())
            
        return base_url
    
    def get_resolutions(self):
        """Return list of video representations with their resolutions."""
        return [
            rep for rep in self.representations
            if rep['type'] == 'video'
        ]

    def get_audios(self):
        """Return list of audio representations."""
        return [
            rep for rep in self.representations
            if rep['type'] == 'audio'
        ]

    def get_best_video(self):
        """Return the best video representation (highest resolution, then bandwidth)."""
        videos = self.get_resolutions()
        if not videos:
            return None
        
        # Sort by (height, width, bandwidth)
        return max(videos, key=lambda r: (r['height'], r['width'], r['bandwidth']))

    def get_best_audio(self):
        """Return the best audio representation (highest bandwidth)."""
        audios = self.get_audios()
        if not audios:
            return None
        return max(audios, key=lambda r: r['bandwidth'])

    def select_video(self, force_resolution="Best"):
        """
        Select a video representation based on the requested resolution.
        Returns: (selected_video, list_available_resolution, filter_custom_resolution, downloadable_video)
        """
        video_reps = self.get_resolutions()
        list_available_resolution = [
            f"{rep['width']}x{rep['height']}" for rep in video_reps
        ]
        force_resolution_l = (force_resolution or "Best").lower()

        if force_resolution_l == "best":
            selected_video = self.get_best_video()
            filter_custom_resolution = "Best"

        elif force_resolution_l == "worst":
            selected_video = MPDParser.get_worst(video_reps)
            filter_custom_resolution = "Worst"

        else:
            selected_video = self.get_best_video()
            filter_custom_resolution = "Best"

        downloadable_video = f"{selected_video['width']}x{selected_video['height']}" if selected_video else "N/A"
        return selected_video, list_available_resolution, filter_custom_resolution, downloadable_video

    def select_audio(self, preferred_audio_langs=None):
        """
        Select an audio representation based on preferred languages.
        Returns: (selected_audio, list_available_audio_langs, filter_custom_audio, downloadable_audio)
        """
        audio_reps = self.get_audios()
        list_available_audio_langs = [
            rep['language'] or "None" for rep in audio_reps
        ]

        selected_audio = None
        filter_custom_audio = "First"

        if preferred_audio_langs:
            
            # Search for the first available language in order of preference
            for lang in preferred_audio_langs:
                for rep in audio_reps:
                    if (rep['language'] or "None").lower() == lang.lower():
                        selected_audio = rep
                        filter_custom_audio = lang
                        break
                if selected_audio:
                    break
            if not selected_audio:
                selected_audio = self.get_best_audio()
        else:
            selected_audio = self.get_best_audio()

        downloadable_audio = selected_audio['language'] or "None" if selected_audio else "N/A"
        return selected_audio, list_available_audio_langs, filter_custom_audio, downloadable_audio