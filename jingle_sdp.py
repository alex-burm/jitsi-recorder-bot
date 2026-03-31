"""
Jingle XML <-> SDP conversion.
Based on lib-jitsi-meet/modules/sdp/SDP.ts and SDPUtil.ts.
"""

import re
import time
import uuid
from xml.etree import ElementTree as ET


# Jingle XML namespaces
NS_JINGLE = "urn:xmpp:jingle:1"
NS_JINGLE_RTP = "urn:xmpp:jingle:apps:rtp:1"
NS_ICE_UDP = "urn:xmpp:jingle:transports:ice-udp:1"
NS_DTLS_SRTP = "urn:xmpp:jingle:apps:dtls:0"
NS_BUNDLE = "urn:ietf:rfc:5888"
NS_SOURCE = "urn:xmpp:jingle:apps:rtp:ssma:0"
NS_RTCP_MUX = "urn:ietf:rfc:5761"
NS_RTP_HDREXT = "urn:xmpp:jingle:apps:rtp:rtp-hdrext:0"


def candidate_from_jingle(cand_elem: ET.Element) -> str:
    """Convert a Jingle <candidate> element to an SDP a=candidate line."""
    foundation = cand_elem.get("foundation", "1")
    component = cand_elem.get("component", "1")
    protocol = cand_elem.get("protocol", "udp").lower()
    priority = cand_elem.get("priority", "0")
    ip = cand_elem.get("ip", "0.0.0.0")
    port = cand_elem.get("port", "0")
    ctype = cand_elem.get("type", "host")
    generation = cand_elem.get("generation", "0")
    network = cand_elem.get("network", "1")

    line = f"candidate:{foundation} {component} {protocol} {priority} {ip} {port} typ {ctype}"

    if ctype in ("srflx", "prflx", "relay"):
        rel_addr = cand_elem.get("rel-addr")
        rel_port = cand_elem.get("rel-port")
        if rel_addr and rel_port:
            line += f" raddr {rel_addr} rport {rel_port}"

    if protocol == "tcp":
        tcptype = cand_elem.get("tcptype", "passive")
        line += f" tcptype {tcptype}"

    line += f" generation {generation}"
    return line


def candidate_to_jingle(sdp_line: str) -> dict:
    """Convert SDP a=candidate line to dict of Jingle attributes."""
    if sdp_line.startswith("a="):
        sdp_line = sdp_line[2:]
    if sdp_line.startswith("candidate:"):
        sdp_line = sdp_line[len("candidate:"):]

    parts = sdp_line.strip().split()
    if len(parts) < 8 or parts[6] != "typ":
        return None

    result = {
        "foundation": parts[0],
        "component": parts[1],
        "protocol": parts[2].lower(),
        "priority": parts[3],
        "ip": parts[4],
        "port": parts[5],
        "type": parts[7],
        "generation": "0",
        "network": "1",
        "id": uuid.uuid4().hex[:10],
    }

    i = 8
    while i < len(parts) - 1:
        key = parts[i]
        val = parts[i + 1]
        if key == "raddr":
            result["rel-addr"] = val
        elif key == "rport":
            result["rel-port"] = val
        elif key == "generation":
            result["generation"] = val
        elif key == "tcptype":
            result["tcptype"] = val
        i += 2

    return result


def jingle_to_sdp(jingle_elem: ET.Element) -> str:
    """
    Convert a Jingle <jingle> element (session-initiate) to an SDP string.
    Returns the full SDP offer string.
    """
    session_id = int(time.time())
    sdp = (
        "v=0\r\n"
        f"o=- {session_id} 2 IN IP4 0.0.0.0\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
    )

    # BUNDLE group
    group_elem = jingle_elem.find(f"group[@{{urn:ietf:rfc:5888}}xmlns]")
    # Try different namespace approaches
    bundle_mids = []
    for group_elem in jingle_elem:
        tag = group_elem.tag
        if "group" in tag:
            semantics = group_elem.get("semantics") or group_elem.get("type", "BUNDLE")
            contents = [c.get("name") for c in group_elem if "content" in c.tag]
            if contents:
                bundle_mids = contents
                sdp += f"a=group:{semantics} {' '.join(contents)}\r\n"
            break

    # If no BUNDLE group found, collect content names
    if not bundle_mids:
        contents_found = [c.get("name") for c in jingle_elem if "content" in c.tag]
        if contents_found:
            sdp += f"a=group:BUNDLE {' '.join(contents_found)}\r\n"

    sdp += "a=msid-semantic: WMS *\r\n"

    # Process each <content>
    for content in jingle_elem:
        if "content" not in content.tag:
            continue
        sdp += _content_to_sdp(content)

    return sdp


def _find_ns(elem: ET.Element, local: str) -> ET.Element:
    """Find child element by local name (ignoring namespace)."""
    for child in elem:
        tag = child.tag
        if tag == local or tag.endswith(f"}}{local}") or tag.endswith(f"]{local}"):
            return child
        # strip namespace
        if "}" in tag:
            local_name = tag.split("}", 1)[1]
            if local_name == local:
                return child
    return None


def _findall_ns(elem: ET.Element, local: str):
    """Find all child elements by local name."""
    result = []
    for child in elem:
        tag = child.tag
        local_name = tag.split("}", 1)[1] if "}" in tag else tag
        if local_name == local:
            result.append(child)
    return result


def _content_to_sdp(content: ET.Element) -> str:
    """Convert a single Jingle <content> element to an SDP media section."""
    name = content.get("name", "audio")
    senders = content.get("senders", "both")

    # Find <description> and <transport>
    desc = None
    transport = None
    for child in content:
        local = child.tag.split("}", 1)[1] if "}" in child.tag else child.tag
        if local == "description":
            desc = child
        elif local == "transport":
            transport = child

    if desc is None:
        return ""

    media_type = desc.get("media", "audio")

    # Collect payload types
    payload_types = _findall_ns(desc, "payload-type")
    fmt_ids = [pt.get("id") for pt in payload_types]

    sdp = ""

    # m= line
    if media_type == "application":
        sdp += f"m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    else:
        fmt_str = " ".join(fmt_ids)
        sdp += f"m={media_type} 9 UDP/TLS/RTP/SAVPF {fmt_str}\r\n"

    sdp += "c=IN IP4 0.0.0.0\r\n"
    sdp += "a=rtcp:1 IN IP4 0.0.0.0\r\n"

    # ICE credentials + fingerprint from transport
    if transport is not None:
        ufrag = transport.get("ufrag")
        pwd = transport.get("pwd")
        if ufrag:
            sdp += f"a=ice-ufrag:{ufrag}\r\n"
        if pwd:
            sdp += f"a=ice-pwd:{pwd}\r\n"
        sdp += "a=ice-options:trickle\r\n"

        # fingerprint
        for fp_elem in transport:
            local_name = fp_elem.tag.split("}", 1)[1] if "}" in fp_elem.tag else fp_elem.tag
            if local_name == "fingerprint":
                hash_algo = fp_elem.get("hash", "sha-256")
                fp_value = fp_elem.text or ""
                setup = fp_elem.get("setup", "actpass")
                sdp += f"a=fingerprint:{hash_algo} {fp_value.strip()}\r\n"
                sdp += f"a=setup:{setup}\r\n"
                break

        # candidates
        for cand_elem in transport:
            local_name = cand_elem.tag.split("}", 1)[1] if "}" in cand_elem.tag else cand_elem.tag
            if local_name == "candidate":
                cand_line = candidate_from_jingle(cand_elem)
                sdp += f"a={cand_line}\r\n"

    # direction
    dir_map = {
        "initiator": "sendonly",
        "responder": "recvonly",
        "none": "inactive",
        "both": "sendrecv",
    }
    direction = dir_map.get(senders, "sendrecv")
    sdp += f"a={direction}\r\n"

    sdp += f"a=mid:{name}\r\n"

    # rtcp-mux
    for child in desc:
        local_name = child.tag.split("}", 1)[1] if "}" in child.tag else child.tag
        if local_name == "rtcp-mux":
            sdp += "a=rtcp-mux\r\n"
            break

    # rtcp-rsize
    for child in desc:
        local_name = child.tag.split("}", 1)[1] if "}" in child.tag else child.tag
        if local_name == "rtcp-fb" and child.get("type") == "nack":
            pass  # handled below with payload types

    # payload types (rtpmap, fmtp, rtcp-fb)
    for pt in payload_types:
        pt_id = pt.get("id")
        pt_name = pt.get("name")
        clockrate = pt.get("clockrate", "48000")
        channels = pt.get("channels")

        rtpmap = f"a=rtpmap:{pt_id} {pt_name}/{clockrate}"
        if channels:
            rtpmap += f"/{channels}"
        sdp += f"{rtpmap}\r\n"

        # fmtp parameters
        params = _findall_ns(pt, "parameter")
        if params:
            fmtp_parts = []
            for param in params:
                pname = param.get("name")
                pval = param.get("value", "")
                if pname:
                    fmtp_parts.append(f"{pname}={pval}")
                else:
                    fmtp_parts.append(pval)
            sdp += f"a=fmtp:{pt_id} {';'.join(fmtp_parts)}\r\n"

        # rtcp-fb
        for fb in _findall_ns(pt, "rtcp-fb"):
            fb_type = fb.get("type")
            fb_subtype = fb.get("subtype")
            if fb_subtype:
                sdp += f"a=rtcp-fb:{pt_id} {fb_type} {fb_subtype}\r\n"
            else:
                sdp += f"a=rtcp-fb:{pt_id} {fb_type}\r\n"

    # RTP header extensions
    for ext in _findall_ns(desc, "rtp-hdrext"):
        ext_id = ext.get("id")
        ext_uri = ext.get("uri")
        if ext_id and ext_uri:
            sdp += f"a=extmap:{ext_id} {ext_uri}\r\n"

    # SSRCs
    for source in _findall_ns(desc, "source"):
        ssrc = source.get("ssrc")
        if ssrc:
            for param in _findall_ns(source, "parameter"):
                pname = param.get("name")
                pval = param.get("value", "")
                sdp += f"a=ssrc:{ssrc} {pname}:{pval}\r\n"

    # SSRC groups (FID, FEC, etc.)
    for sg in _findall_ns(desc, "ssrc-group"):
        semantics = sg.get("semantics", "FID")
        sources = [s.get("ssrc") for s in _findall_ns(sg, "source")]
        if sources:
            sdp += f"a=ssrc-group:{semantics} {' '.join(sources)}\r\n"

    return sdp


def sdp_to_jingle(
    sdp: str,
    sid: str,
    initiator: str,
    responder: str,
    content_name_order: list = None,
) -> str:
    """
    Convert an SDP answer string to a Jingle session-accept XML string.
    Returns XML string for the <jingle> element content.
    """
    lines = sdp.replace("\r\n", "\n").split("\n")

    # Parse media sections
    session_lines = []
    media_sections = []
    current_section = None

    for line in lines:
        if line.startswith("m="):
            if current_section is not None:
                media_sections.append(current_section)
            current_section = [line]
        elif current_section is not None:
            current_section.append(line)
        else:
            session_lines.append(line)

    if current_section:
        media_sections.append(current_section)

    # Extract session-level ICE/fingerprint (usually per-media in WebRTC)
    def get_val(lines_list, prefix):
        for l in lines_list:
            if l.startswith(prefix):
                return l[len(prefix):]
        return None

    # Build Jingle XML
    contents_xml = []

    # Collect BUNDLE mids from session
    bundle_mids = []
    for line in session_lines:
        if line.startswith("a=group:BUNDLE "):
            bundle_mids = line[len("a=group:BUNDLE "):].split()
            break

    used_content_names = []

    for idx, section in enumerate(media_sections):
        m_line = section[0]
        # m=audio 9 UDP/TLS/RTP/SAVPF 111 ...
        m_parts = m_line[2:].split()
        media_type = m_parts[0]

        mid = get_val(section, "a=mid:")
        if not mid:
            mid = media_type

        content_name = mid
        if content_name_order and idx < len(content_name_order):
            content_name = content_name_order[idx]

        # In Jingle: senders= refers to who sends, from both sides' perspective.
        # For the responder's answer SDP:
        #   a=recvonly → responder only receives → initiator sends → senders="initiator"
        #   a=sendonly → responder only sends → responder sends → senders="responder"
        #   a=sendrecv → both send → senders="both"
        #   a=inactive → nobody → senders="none"
        direction = "both"
        for line in section:
            if line == "a=sendrecv":
                direction = "both"
            elif line == "a=sendonly":
                direction = "responder"
            elif line == "a=recvonly":
                direction = "initiator"
            elif line == "a=inactive":
                direction = "none"

        ufrag = get_val(section, "a=ice-ufrag:")
        pwd = get_val(section, "a=ice-pwd:")
        fingerprint_line = get_val(section, "a=fingerprint:")
        setup = get_val(section, "a=setup:")

        fp_hash = ""
        fp_val = ""
        if fingerprint_line:
            fp_parts = fingerprint_line.split(" ", 1)
            fp_hash = fp_parts[0]
            fp_val = fp_parts[1] if len(fp_parts) > 1 else ""

        # Collect rtpmaps
        rtpmaps = {}
        fmtps = {}
        rtcp_fbs = {}
        candidates = []
        extmaps = []
        has_rtcp_mux = False

        for line in section:
            if line.startswith("a=rtpmap:"):
                val = line[9:]
                pt_id, rest = val.split(" ", 1)
                rtpmaps[pt_id] = rest
            elif line.startswith("a=fmtp:"):
                val = line[7:]
                pt_id, rest = val.split(" ", 1)
                fmtps[pt_id] = rest
            elif line.startswith("a=rtcp-fb:"):
                val = line[10:]
                parts = val.split(" ", 2)
                pt_id = parts[0]
                if pt_id not in rtcp_fbs:
                    rtcp_fbs[pt_id] = []
                rtcp_fbs[pt_id].append(parts[1:])
            elif line.startswith("a=candidate:"):
                cand = candidate_to_jingle(line)
                if cand:
                    candidates.append(cand)
            elif line.startswith("a=extmap:"):
                extmaps.append(line[9:])
            elif line == "a=rtcp-mux":
                has_rtcp_mux = True

        # Build description XML
        desc_xml = f'<description xmlns="{NS_JINGLE_RTP}" media="{media_type}">'

        if has_rtcp_mux:
            desc_xml += "<rtcp-mux/>"

        # Payload types - only those in the m= line fmt list
        fmt_ids = m_parts[3:] if len(m_parts) > 3 else []
        for pt_id in fmt_ids:
            if pt_id not in rtpmaps:
                continue
            rtp_rest = rtpmaps[pt_id]
            # rtp_rest = "opus/48000/2" or "VP8/90000"
            rtp_parts = rtp_rest.split("/")
            pt_name = rtp_parts[0]
            clockrate = rtp_parts[1] if len(rtp_parts) > 1 else "90000"
            channels = rtp_parts[2] if len(rtp_parts) > 2 else None

            attrs = f'id="{pt_id}" name="{pt_name}" clockrate="{clockrate}"'
            if channels:
                attrs += f' channels="{channels}"'
            desc_xml += f"<payload-type {attrs}>"

            # fmtp
            if pt_id in fmtps:
                for param in fmtps[pt_id].split(";"):
                    param = param.strip()
                    if "=" in param:
                        k, v = param.split("=", 1)
                        desc_xml += f'<parameter name="{k.strip()}" value="{v.strip()}"/>'
                    elif param:
                        desc_xml += f'<parameter name="" value="{param}"/>'

            # rtcp-fb
            if pt_id in rtcp_fbs:
                for fb_parts in rtcp_fbs[pt_id]:
                    fb_type = fb_parts[0] if fb_parts else ""
                    fb_sub = fb_parts[1] if len(fb_parts) > 1 else None
                    if fb_sub:
                        desc_xml += f'<rtcp-fb xmlns="urn:xmpp:jingle:apps:rtp:rtcp-fb:0" type="{fb_type}" subtype="{fb_sub}"/>'
                    else:
                        desc_xml += f'<rtcp-fb xmlns="urn:xmpp:jingle:apps:rtp:rtcp-fb:0" type="{fb_type}"/>'

            desc_xml += "</payload-type>"

        # RTP header extensions
        for extmap in extmaps:
            parts = extmap.split(" ", 1)
            ext_id = parts[0]
            ext_uri = parts[1] if len(parts) > 1 else ""
            desc_xml += f'<rtp-hdrext xmlns="{NS_RTP_HDREXT}" id="{ext_id}" uri="{ext_uri}" senders="both"/>'

        desc_xml += "</description>"

        # Build transport XML
        transport_xml = f'<transport xmlns="{NS_ICE_UDP}"'
        if ufrag:
            transport_xml += f' ufrag="{ufrag}"'
        if pwd:
            transport_xml += f' pwd="{pwd}"'
        transport_xml += ">"

        if fp_hash and fp_val:
            actual_setup = setup or "active"
            transport_xml += (
                f'<fingerprint xmlns="{NS_DTLS_SRTP}" hash="{fp_hash}" setup="{actual_setup}">'
                f"{fp_val}</fingerprint>"
            )

        for cand in candidates:
            attrs = " ".join(f'{k}="{v}"' for k, v in cand.items())
            transport_xml += f"<candidate {attrs}/>"

        transport_xml += "</transport>"

        content_xml = (
            f'<content creator="responder" name="{content_name}" senders="{direction}">'
            f"{desc_xml}"
            f"{transport_xml}"
            f"</content>"
        )
        contents_xml.append(content_xml)
        used_content_names.append(content_name)

    # Build group element
    if used_content_names:
        group_xml = f'<group xmlns="{NS_BUNDLE}" semantics="BUNDLE">'
        for name in used_content_names:
            group_xml += f'<content name="{name}"/>'
        group_xml += "</group>"
    else:
        group_xml = ""

    jingle_xml = (
        f'<jingle xmlns="{NS_JINGLE}" action="session-accept" '
        f'initiator="{initiator}" responder="{responder}" sid="{sid}">'
        f"{group_xml}"
        + "".join(contents_xml)
        + "</jingle>"
    )

    return jingle_xml
