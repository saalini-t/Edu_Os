"""SYNTHETIC supplementary Computer Networks notes (original text written for EduOS evaluation only).
Used to enlarge the retrieval-evaluation corpus; it is NOT part of the demo seed."""

SUPPLEMENT_PAGES = [
    ("The Application Layer and HTTP", [
        "HTTP is a request-response protocol used by web browsers and servers. A client sends a request containing a method, a URL and headers, and the server answers with a status code, headers and usually a body. "
        "The GET method retrieves a resource, while the POST method submits data to the server for processing.",
        "Status codes are grouped by their first digit. Codes beginning with 2 report success, codes beginning with 3 report redirection, codes beginning with 4 report a client error and codes beginning with 5 report a server error. "
        "For example, 404 means that the requested resource was not found.",
        "HTTP is stateless, which means that the server does not remember earlier requests by itself. Cookies add state by letting the server store a small token in the browser that is sent back with later requests. "
        "HTTP/1.1 uses persistent connections, so several requests can reuse one TCP connection instead of paying for a new handshake each time. "
        "Conditional requests let a client ask for a resource only if it changed, which allows caches to save bandwidth.",
    ]),
    ("The Domain Name System", [
        "The Domain Name System, or DNS, translates human readable host names into IP addresses. The name space is hierarchical, with root servers at the top, top-level domain servers below them and authoritative servers that hold the records for a particular domain.",
        "A client normally asks a recursive resolver to find an answer. The resolver contacts the root, top-level domain and authoritative servers in turn and returns the final answer. "
        "Resolvers cache answers for the time given by the time to live of the record, abbreviated TTL, so repeated lookups are fast.",
        "Common record types are A records for IPv4 addresses, AAAA records for IPv6 addresses, CNAME records for aliases, MX records for mail servers and NS records for name servers. "
        "DNS queries usually use UDP port 53 because the messages are small, and fall back to TCP for large responses.",
    ]),
    ("UDP and Port Numbers", [
        "UDP provides a connectionless and unreliable datagram service. There is no handshake, no retransmission and no congestion control. The UDP header is only 8 bytes long and contains the source port, the destination port, a length and a checksum.",
        "Applications that value low delay over reliability use UDP, for example DNS lookups, voice calls and online games. An application that needs reliability on top of UDP must provide it itself.",
        "Port numbers identify the process on a host that should receive a segment. Well-known ports are below 1024, for example 80 for HTTP, 443 for HTTPS and 53 for DNS. "
        "A TCP connection is identified by four values, the source IP address, the source port, the destination IP address and the destination port.",
    ]),
    ("Link Layer: Ethernet, Switches and ARP", [
        "Ethernet frames carry 48 bit MAC addresses that identify network interfaces on a local network. The maximum payload of an Ethernet frame is normally 1500 bytes, which is called the maximum transmission unit, or MTU.",
        "A switch learns which MAC address can be reached through which port by reading the source address of every frame it receives. It stores this in a switch table and forwards later frames only to the correct port. "
        "If the destination address is not yet in the table, the switch floods the frame out of all other ports.",
        "The Address Resolution Protocol, ARP, maps an IP address to a MAC address on the same network. A host broadcasts an ARP request asking who owns an IP address, and the owner replies with its MAC address. The answer is cached for a short time.",
    ]),
    ("DHCP and NAT", [
        "The Dynamic Host Configuration Protocol, DHCP, gives a host an IP address, a subnet mask, a default gateway and a DNS server automatically. "
        "The exchange has four steps called discover, offer, request and acknowledge.",
        "Network Address Translation, NAT, lets many hosts that use private addresses share a single public address. The NAT router rewrites the source address and port of outgoing packets and records the mapping in a translation table, so that it can rewrite the replies. "
        "The private ranges are 10.0.0.0/8, 172.16.0.0/12 and 192.168.0.0/16.",
        "Because the mapping is created by outgoing traffic, NAT makes it hard for outside hosts to start connections to a host behind the router.",
    ]),
    ("Transport Layer Security", [
        "Transport Layer Security, TLS, provides confidentiality, integrity and authentication for data sent over TCP. HTTPS is simply HTTP carried over TLS and normally uses port 443.",
        "In the TLS handshake the client sends the versions and cipher suites it supports, and the server answers with its certificate. The client verifies the certificate chain up to a certificate authority that it trusts. "
        "The two sides then perform a key exchange to derive shared session keys.",
        "After the handshake the data is protected with symmetric encryption, because symmetric ciphers are much faster than public key cryptography. Authentication of the server prevents an attacker from impersonating it.",
    ]),
    ("Delay, Throughput and the Bandwidth-Delay Product", [
        "The delay of a packet on a path has four components, processing delay, queuing delay, transmission delay and propagation delay. "
        "Transmission delay is the packet length divided by the link rate, while propagation delay is the link length divided by the propagation speed of the signal.",
        "Queuing delay grows when packets arrive faster than the link can send them, and it is the main source of variable delay in a congested router. When the queue is full, the router drops packets.",
        "The bandwidth-delay product is the link bandwidth multiplied by the round-trip time. It equals the amount of data that must be in flight to keep the path completely full, so a sender with a smaller window cannot use the full capacity of the path.",
    ]),
]
