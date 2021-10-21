import math
import os
import time
import numpy as np
import scipy.signal as sps
import copy
from collections import namedtuple

import lddecode.core as ldd
import lddecode.utils as lddu
from lddecode.utils import unwrap_hilbert, inrange
import vhsdecode.utils as utils
from vhsdecode.utils import StackableMA
from vhsdecode.chroma import (
    decode_chroma_vhs,
    decode_chroma_umatic,
    demod_chroma_filt,
    try_detect_track_vhs_pal,
    try_detect_track_vhs_ntsc,
    get_field_phase_id,
)
from vhsdecode.doc import detect_dropouts_rf

import vhsdecode.formats as vhs_formats
from vhsdecode.addons.FMdeemph import FMDeEmphasis
from vhsdecode.addons.FMdeemph import FMDeEmphasisB
from vhsdecode.addons.chromasep import ChromaSepClass
from vhsdecode.addons.resync import Resync
from vhsdecode.addons.chromaAFC import ChromaAFC

from numba import njit

# Use PyFFTW's faster FFT implementation if available
try:
    import pyfftw.interfaces.numpy_fft as npfft
    import pyfftw.interfaces

    pyfftw.interfaces.cache.enable()
    pyfftw.interfaces.cache.set_keepalive_time(10)
except ImportError:
    import numpy.fft as npfft


@njit(cache=True)
def replace_spikes(demod, demod_diffed, max_value, replace_start=8, replace_end=30):
    """Go through and replace spikes and some samples after them with data
    from the diff demod pass"""
    assert len(demod) == len(
        demod_diffed
    ), "diff demod length doesn't match demod length"
    too_high = max_value
    to_fix = np.where(demod > too_high)[0]

    for i in to_fix:
        start = max(i - replace_start, 0)
        end = min(i + replace_end, len(demod_diffed) - 1)
        demod[start:end] = demod_diffed[start:end]

    return demod


def getpulses_override(field):
    return field.rf.resync.getpulses_override(field)


# def ynr(data, hpfdata, line_len):
#     """Dumb vcr-line ynr
#     """

#     numlines = len(data) // line_len
#     hpfdata = np.clip(hpfdata, -7000, 7000)
#     for line_num in range(16, numlines - 2):
#         delayed1h = hpfdata[(line_num - 1) * line_len : (line_num) * line_len]
#         line_slice = hpfdata[line_num * line_len : (line_num + 1) * line_len]
#         adv1h = hpfdata[(line_num + 1) * line_len : (line_num + 2) * line_len]
#         # Let the delayed signal contribute 1/3.
#         # Could probably make the filtering configurable later.
#         data[line_num * line_len : (line_num + 1) * line_len] -= line_slice
#         data[line_num * line_len : (line_num + 1) * line_len] += (((delayed1h + line_slice + adv1h) / 3) - line_slice)
#     return data


class FieldShared:
    def refinepulses(self):
        LT = self.get_timings()

        HSYNC, EQPL1, VSYNC, EQPL2 = range(4)

        i = 0
        valid_pulses = []
        num_vblanks = 0

        Pulse = namedtuple("Pulse", "start len")

        while i < len(self.rawpulses):
            curpulse = self.rawpulses[i]
            if inrange(curpulse.len, *LT["hsync"]):
                good = (
                    self.pulse_qualitycheck(valid_pulses[-1], (0, curpulse))
                    if len(valid_pulses)
                    else False
                )
                valid_pulses.append((HSYNC, curpulse, good))
                i += 1
            elif inrange(curpulse.len, LT["hsync"][1], LT["hsync"][1] * 3):
                # If the pulse is longer than expected, we could have ended up detecting the back
                # porch as sync.
                # try to move a bit lower to see if we hit a hsync.
                data = self.data["video"]["demod_05"][
                    curpulse.start : curpulse.start + curpulse.len
                ]
                threshold = self.rf.iretohz(self.rf.hztoire(data[0]) - 10)
                pulses = self.rf.resync.findpulses(data, 0, threshold)
                if len(pulses):
                    newpulse = Pulse(curpulse.start + pulses[0].start, pulses[0].len)
                    self.rawpulses[i] = newpulse
                    curpulse = newpulse
                else:
                    spulse = (HSYNC, self.rawpulses[i], False)
                    i += 1
            elif (
                i > 2
                and inrange(self.rawpulses[i].len, *LT["eq"])
                and (len(valid_pulses) and valid_pulses[-1][0] == HSYNC)
            ):
                # print(i, self.rawpulses[i])
                done, vblank_pulses = self.run_vblank_state_machine(
                    self.rawpulses[i - 2 : i + 24], LT
                )
                if done:
                    [valid_pulses.append(p) for p in vblank_pulses[2:]]
                    i += len(vblank_pulses) - 2
                    num_vblanks += 1
                else:
                    spulse = (HSYNC, self.rawpulses[i], False)
                    i += 1
            else:
                spulse = (HSYNC, self.rawpulses[i], False)
                i += 1

        return valid_pulses  # , num_vblanks

    def compute_linelocs(self):
        self.rawpulses = self.getpulses()
        if self.rawpulses is None or len(self.rawpulses) == 0:
            ldd.logger.error("Unable to find any sync pulses, jumping 100 ms")
            return None, None, int(self.rf.freq_hz / 10)

        self.validpulses = validpulses = self.refinepulses()

        line0loc, lastlineloc, self.isFirstField = self.getLine0(validpulses)
        # Not sure if this is used for video.
        self.linecount = 263 if self.isFirstField else 262

        # Number of lines to actually process.  This is set so that the entire following
        # VSYNC is processed
        proclines = self.outlinecount + self.lineoffset + 10
        if self.rf.system == "PAL":
            proclines += 3

        # It's possible for getLine0 to return None for lastlineloc
        if lastlineloc is not None:
            numlines = (lastlineloc - line0loc) / self.inlinelen
            self.skipdetected = numlines < (self.linecount - 5)
        else:
            self.skipdetected = False

        linelocs_dict = {}
        linelocs_dist = {}

        if line0loc is None:
            if self.initphase is False:
                ldd.logger.error("Unable to determine start of field - dropping field")
            return None, None, self.inlinelen * 100

        meanlinelen = self.computeLineLen(validpulses)
        self.meanlinelen = meanlinelen

        # If we don't have enough data at the end, move onto the next field
        lastline = (self.rawpulses[-1].start - line0loc) / meanlinelen
        if lastline < proclines:
            if self.prevfield is not None:
                ldd.logger.info("lastline < proclines , skipping a tiny bit")
            return None, None, max(line0loc - (meanlinelen * 20), self.inlinelen)

        for p in validpulses:
            lineloc = (p[1].start - line0loc) / meanlinelen
            rlineloc = ldd.nb_round(lineloc)
            lineloc_distance = np.abs(lineloc - rlineloc)

            if self.skipdetected:
                lineloc_end = self.linecount - (
                    (lastlineloc - p[1].start) / meanlinelen
                )
                rlineloc_end = ldd.nb_round(lineloc_end)
                lineloc_end_distance = np.abs(lineloc_end - rlineloc_end)

                if (
                    p[0] == 0
                    and rlineloc > 23
                    and lineloc_end_distance < lineloc_distance
                ):
                    lineloc = lineloc_end
                    rlineloc = rlineloc_end
                    lineloc_distance = lineloc_end_distance

            # only record if it's closer to the (probable) beginning of the line
            if lineloc_distance > self.rf.hsync_tolerance or (
                rlineloc in linelocs_dict and lineloc_distance > linelocs_dist[rlineloc]
            ):
                continue

            # also skip non-regular lines (non-hsync) that don't seem to be in valid order (p[2])
            # (or hsync lines in the vblank area)
            if rlineloc > 0 and not p[2]:
                if p[0] > 0 or (p[0] == 0 and rlineloc < 10):
                    continue

            linelocs_dict[rlineloc] = p[1].start
            linelocs_dist[rlineloc] = lineloc_distance

        rv_err = np.full(proclines, False)

        # Convert dictionary into list, then fill in gaps
        linelocs = [
            linelocs_dict[l] if l in linelocs_dict else -1 for l in range(0, proclines)
        ]
        linelocs_filled = linelocs.copy()

        self.linelocs0 = linelocs.copy()

        if linelocs_filled[0] < 0:
            # logger.info("linelocs_filled[0] < 0, %s", linelocs_filled)
            next_valid = None
            for i in range(0, self.outlinecount + 1):
                if linelocs[i] > 0:
                    next_valid = i
                    break

            if next_valid is None:
                # If we don't find anything valid, guess something to avoid dropping fields
                prev_line0 = (
                    np.int64(self.prevfield.linelocs0[0])
                    if self.prevfield is not None
                    else 0
                )

                if prev_line0 > 0:
                    linelocs_filled = self.prevfield.linelocs0 - prev_line0 + line0loc
                else:
                    linelocs_filled[0] = line0loc
                linelocs = linelocs_filled
                ldd.logger.warning(
                    "no valid lines found! Guessing or using values for previous field so result will probably be garbled!"
                )
                rv_err[1:] = True
            else:
                linelocs_filled[0] = linelocs_filled[next_valid] - (
                    next_valid * meanlinelen
                )

            if linelocs_filled[0] < self.inlinelen:
                ldd.logger.info("linelocs_filled[0] too short! %s", self.inlinelen)
                return None, None, line0loc + (self.inlinelen * self.outlinecount - 7)

        for l in range(1, proclines):
            if linelocs_filled[l] < 0:
                rv_err[l] = True

                prev_valid = None
                next_valid = None

                for i in range(l, -1, -1):
                    if linelocs[i] > 0:
                        prev_valid = i
                        break
                for i in range(l, self.outlinecount + 1):
                    if linelocs[i] > 0:
                        next_valid = i
                        break

                if prev_valid is None:
                    avglen = self.inlinelen
                    linelocs_filled[l] = linelocs[next_valid] - (
                        avglen * (next_valid - l)
                    )
                elif next_valid is not None:
                    avglen = (linelocs[next_valid] - linelocs[prev_valid]) / (
                        next_valid - prev_valid
                    )
                    linelocs_filled[l] = linelocs[prev_valid] + (
                        avglen * (l - prev_valid)
                    )
                else:
                    avglen = self.inlinelen
                    linelocs_filled[l] = linelocs[prev_valid] + (
                        avglen * (l - prev_valid)
                    )

        # *finally* done :)

        rv_ll = [linelocs_filled[l] for l in range(0, proclines)]

        # ldd.logger.info("line0loc %s %s", int(line0loc), int(self.meanlinelen))

        self.data["video"]["demod"][
            int(line0loc) : int(line0loc + (self.meanlinelen) * 4)
        ] = 5e6

        if False:
            # len(validpulses) > 300:
            import matplotlib.pyplot as plt

            fig, (ax1) = plt.subplots(1, 1, sharex=True)
            ax1.plot(self.data["video"]["demod"])

            sync, blank = self.rf.resync.VsyncSerration.getLevels()

            ax1.axhline(sync, color="#FF0000")
            ax1.axhline(blank, color="#00FF00")

            # for raw_pulse in self.rawpulses:
            #     ax1.axvline(raw_pulse.start, color="#910000")
            #     ax1.axvline(raw_pulse.start + raw_pulse.len, color="#090909")

            for valid_pulse in validpulses:
                if valid_pulse[0] != 0:
                    color = "#FF0000" if valid_pulse[0] == 2 else "#00FF00"
                    ax1.axvline(valid_pulse[1][0], color=color)
                    ax1.axvline(valid_pulse[1][0] + valid_pulse[1][1], color="#009900")

            ax1.axvline(line0loc, color="000000")

            # ax2.plot(np.diff(self.data["video"]["demod_05"]))

            # pulselen = np.zeros_like(self.data["video"]["demod_05"])
            # for valid_pulse in validpulses:
            #     pulselen[valid_pulse[1][0]:valid_pulse[1][0] + valid_pulse[1][1]] = valid_pulse[1][1]

            # ax2.plot(pulselen)

            # for p in linelocs_dict.values():
            #    ax3.axvline(p)
            # ldd.logger.info("p %s", p)
            # for ll in rv_ll:
            #     ax3.axvline(ll)

            # ax1.axhline(self.pulse_hz_min, color="#00FFFF")
            # ax1.axhline(self.pulse_hz_max, color="#000000")
            plt.show()

        if self.vblank_next is None:
            nextfield = linelocs_filled[self.outlinecount - 7]
        else:
            nextfield = self.vblank_next - (self.inlinelen * 8)

        return rv_ll, rv_err, nextfield

    def getBlankRange(self, validpulses, start=0):
        """Look through pulses to fit a group that fit as a blanking area.
        Overridden to lower the threshold a little as the default
        discarded some distorted/non-standard ones.
        """
        vp_type = np.array([p[0] for p in validpulses])

        vp_vsyncs = np.where(vp_type[start:] == ldd.VSYNC)[0]
        firstvsync = vp_vsyncs[0] + start if len(vp_vsyncs) else None

        if firstvsync is None or firstvsync < 10:
            if start == 0:
                ldd.logger.debug("No vsync found!")
            return None, None

        for newstart in range(firstvsync - 10, firstvsync - 4):
            blank_locs = np.where(vp_type[newstart:] > 0)[0]
            if len(blank_locs) == 0:
                continue

            firstblank = blank_locs[0] + newstart
            hsync_locs = np.where(vp_type[firstblank:] == 0)[0]

            if len(hsync_locs) == 0:
                continue

            lastblank = hsync_locs[0] + firstblank - 1

            if (lastblank - firstblank) > vhs_formats.BLANK_LENGTH_THRESHOLD:
                return firstblank, lastblank

        # there isn't a valid range to find, or it's impossibly short
        return None, None

    def calc_burstmedian(self):
        # Set this to a constant value for now to avoid the comb filter messing with chroma levels.
        return 1.0

    def getpulses(self):
        """Find sync pulses in the demodulated video signal

        NOTE: TEMPORARY override until an override for the value itself is added upstream.
        """
        return getpulses_override(self)

    def compute_deriv_error(self, linelocs, baserr):
        """Disabled this for now as tapes have large variations in line pos
        Due to e.g head switch.
        compute errors based off the second derivative - if it exceeds 1 something's wrong,
        and if 4 really wrong...
        """
        return baserr

    def dropout_detect(self):
        return detect_dropouts_rf(self)

    def get_timings(self):
        """Get the expected length and tolerance for sync pulses. Overriden to allow wider tolerance."""

        # Get the defaults - this works somehow because python.
        LT = super(FieldShared, self).get_timings()

        hsync_min = LT["hsync_median"] + self.usectoinpx(-0.7)
        hsync_max = LT["hsync_median"] + self.usectoinpx(0.7)

        LT["hsync"] = (hsync_min, hsync_max)

        eq_min = (
            self.usectoinpx(
                self.rf.SysParams["eqPulseUS"] - vhs_formats.EQ_PULSE_TOLERANCE
            )
            + LT["hsync_offset"]
        )
        eq_max = (
            self.usectoinpx(
                self.rf.SysParams["eqPulseUS"] + vhs_formats.EQ_PULSE_TOLERANCE
            )
            + LT["hsync_offset"]
        )

        LT["eq"] = (eq_min, eq_max)

        return LT

    def computewow(self, lineinfo):
        """Compute how much the line deviates fron expected
        Overridden to limit the result so we don't get super-bright lines at head switch.
        TODO: Better solution to that
        """
        wow = np.ones(len(lineinfo))

        for l in range(0, len(wow) - 1):
            wow[l] = min(self.get_linelen(l) / self.inlinelen, 1.06)

        for l in range(self.lineoffset, self.lineoffset + 10):
            wow[l] = np.median(wow[l : l + 4])

        return wow


class FieldPALShared(FieldShared, ldd.FieldPAL):
    def __init__(self, *args, **kwargs):
        super(FieldPALShared, self).__init__(*args, **kwargs)

    def refine_linelocs_pilot(self, linelocs=None):
        """Override this as most regular band tape formats does not use have a pilot burst.
        Tape formats that do have it will need separate logic for it anyhow.
        """
        if linelocs is None:
            linelocs = self.linelocs2.copy()
        else:
            linelocs = linelocs.copy()

        return linelocs

    def determine_field_number(self):
        """Workaround to shut down phase id mismatch warnings, the actual code
        doesn't work properly with the vhs output at the moment."""
        return 1 + (self.rf.field_number % 8)


class FieldPALVHS(FieldPALShared):
    def __init__(self, *args, **kwargs):
        super(FieldPALVHS, self).__init__(*args, **kwargs)

    def downscale(self, final=False, *args, **kwargs):
        dsout, dsaudio, dsefm = super(FieldPALVHS, self).downscale(
            False, *args, **kwargs
        )
        dschroma = decode_chroma_vhs(self)
        # hpf = utils.filter_simple(dsout, self.rf.Filters["NLHighPass"])
        # dsout = ynr(dsout, hpf, self.outlinelen)

        if final:
            dsout = self.hz_to_output(dsout)
            self.dspicture = dsout

        return (dsout, dschroma), dsaudio, dsefm

    def try_detect_track(self):
        return try_detect_track_vhs_pal(self)


class FieldPALSVHS(FieldPALVHS):
    """Add PAL SVHS-specific stuff (deemp, pilot burst etc here)"""

    def __init__(self, *args, **kwargs):
        super(FieldPALSVHS, self).__init__(*args, **kwargs)


class FieldPALUMatic(FieldPALShared):
    def __init__(self, *args, **kwargs):
        super(FieldPALUMatic, self).__init__(*args, **kwargs)

    def downscale(self, final=False, *args, **kwargs):
        dsout, dsaudio, dsefm = super(FieldPALUMatic, self).downscale(
            final, *args, **kwargs
        )
        dschroma = decode_chroma_umatic(self)

        return (dsout, dschroma), dsaudio, dsefm


class FieldNTSCShared(FieldShared, ldd.FieldNTSC):
    def __init__(self, *args, **kwargs):
        super(FieldNTSCShared, self).__init__(*args, **kwargs)
        self.fieldPhaseID = 0

    def refine_linelocs_burst(self, linelocs=None):
        """Override this as it's LD specific
        At some point in the future we could maybe use the burst location to improve hsync accuracy,
        but ignore it for now.
        """
        if linelocs is None:
            linelocs = self.linelocs2
        else:
            linelocs = linelocs.copy()

        return linelocs


class FieldNTSCVHS(FieldNTSCShared):
    def __init__(self, *args, **kwargs):
        super(FieldNTSCVHS, self).__init__(*args, **kwargs)

    def try_detect_track(self):
        return try_detect_track_vhs_ntsc(self)

    def downscale(self, linesoffset=0, final=False, *args, **kwargs):
        """Downscale the channels and upconvert chroma to standard color carrier frequency."""
        dsout, dsaudio, dsefm = super(FieldNTSCVHS, self).downscale(
            linesoffset, final, *args, **kwargs
        )

        dschroma = decode_chroma_vhs(self)

        self.fieldPhaseID = get_field_phase_id(self)

        return (dsout, dschroma), dsaudio, dsefm


class FieldMPALVHS(FieldNTSCVHS):
    def __init__(self, *args, **kwargs):
        super(FieldMPALVHS, self).__init__(*args, **kwargs)

    def try_detect_track(self):
        return try_detect_track_vhs_pal(self)


class FieldNTSCUMatic(FieldNTSCShared):
    def __init__(self, *args, **kwargs):
        super(FieldNTSCUMatic, self).__init__(*args, **kwargs)

    def downscale(self, linesoffset=0, final=False, *args, **kwargs):
        dsout, dsaudio, dsefm = super(FieldNTSCUMatic, self).downscale(
            linesoffset, final, *args, **kwargs
        )
        dschroma = decode_chroma_umatic(self)

        self.fieldPhaseID = get_field_phase_id(self)

        return (dsout, dschroma), dsaudio, dsefm


def parent_system(system):
    if system == "MPAL":
        parent_system = "NTSC"
    else:
        parent_system = system
    return parent_system


# Superclass to override laserdisc-specific parts of ld-decode with stuff that works for VHS
#
# We do this simply by using inheritance and overriding functions. This results in some redundant
# work that is later overridden, but avoids altering any ld-decode code to ease merging back in
# later as the ld-decode is in flux at the moment.
class VHSDecode(ldd.LDdecode):
    def __init__(
        self,
        fname_in,
        fname_out,
        freader,
        logger,
        system="NTSC",
        tape_format="VHS",
        doDOD=True,
        threads=1,
        inputfreq=40,
        level_adjust=0,
        rf_options={},
        extra_options={},
    ):

        super(VHSDecode, self).__init__(
            fname_in,
            fname_out,
            freader,
            logger,
            analog_audio=False,
            system=parent_system(system),
            doDOD=doDOD,
            threads=threads,
            extra_options=extra_options,
        )
        # Adjustment for output to avoid clipping.
        self.level_adjust = level_adjust
        # Overwrite the rf decoder with the VHS-altered one
        self.rf = VHSRFDecode(
            system=system,
            tape_format=tape_format,
            inputfreq=inputfreq,
            rf_options=rf_options,
            extra_options=extra_options,
        )
        self.rf.chroma_last_field = -1
        self.rf.chroma_tbc_buffer = np.array([])
        # Store reference to ourself in the rf decoder - needed to access data location for track
        # phase, may want to do this in a better way later.
        self.rf.decoder = self
        if system == "PAL":
            if tape_format == "UMATIC":
                self.FieldClass = FieldPALUMatic
            elif tape_format == "SVHS":
                self.FieldClass = FieldPALSVHS
            else:
                self.FieldClass = FieldPALVHS
        elif system == "NTSC":
            if tape_format == "UMATIC":
                self.FieldClass = FieldNTSCUMatic
            else:
                self.FieldClass = FieldNTSCVHS
        elif system == "MPAL" and tape_format == "VHS":
            self.FieldClass = FieldMPALVHS
        else:
            raise Exception("Unknown video system!", system)

        self.demodcache = ldd.DemodCache(
            self.rf,
            self.infile,
            self.freader,
            num_worker_threads=self.numthreads,
        )

        if fname_out is not None:
            self.outfile_chroma = open(fname_out + "_chroma.tbc", "wb")
        else:
            self.outfile_chroma = None

    # Override to avoid NaN in JSON.
    def calcsnr(self, f, snrslice, psnr=False):
        # if dspicture isn't converted to float, this underflows at -40IRE
        data = f.output_to_ire(f.dspicture[snrslice].astype(float))

        signal = np.mean(data) if not psnr else 100
        noise = np.std(data)
        # Make sure signal is positive so we don't try to do log on a negative value.
        if signal < 0.0:
            ldd.logger.info(
                "WARNING: Negative mean for SNR, changing to absolute value."
            )
            signal = abs(signal)
        if noise == 0:
            return 0
        return 20 * np.log10(signal / noise)

    def buildmetadata(self, f):
        if math.isnan(f.burstmedian):
            f.burstmedian = 0.0
        return super(VHSDecode, self).buildmetadata(f)

    # For laserdisc this decodes frame numbers from VBI metadata, but there won't be such a thing on
    # VHS, so just skip it.
    def decodeFrameNumber(self, f1, f2):
        return None

    # Again ignored for tapes
    def checkMTF(self, field, pfield=None):
        return True

    def writeout(self, dataset):
        f, fi, (picturey, picturec), audio, efm = dataset

        fi["audioSamples"] = 0
        self.fieldinfo.append(fi)

        self.outfile_video.write(picturey)
        self.outfile_chroma.write(picturec)
        self.fields_written += 1

    def close(self):
        setattr(self, "outfile_chroma", None)
        super(VHSDecode, self).close()

    def computeMetricsPAL(self, metrics, f, fp=None):
        return None

    def computeMetricsNTSC(self, metrics, f, fp=None):
        return None

    def build_json(self, f):
        try:
            jout = super(VHSDecode, self).build_json(f)

            black = jout["videoParameters"]["black16bIre"]
            white = jout["videoParameters"]["white16bIre"]

            if self.rf.color_system == "MPAL":
                jout["videoParameters"]["isSourcePal"] = True
                jout["videoParameters"]["isSourcePalM"] = True

            jout["videoParameters"]["black16bIre"] = black * (1 - self.level_adjust)
            jout["videoParameters"]["white16bIre"] = white * (1 + self.level_adjust)
            return jout
        except TypeError as e:
            print("Cannot build json: %s" % e)
            return None

    def readfield(self, initphase=False):
        # pretty much a retry-ing wrapper around decodefield with MTF checking
        self.prevfield = self.curfield
        done = False
        adjusted = False
        redo = False

        while done == False:
            if redo:
                # Only allow one redo, no matter what
                done = True

            self.fieldloc = self.fdoffset
            f, offset = self.decodefield(initphase=initphase)

            if f is None:
                if offset is None:
                    # EOF, probably
                    return None

            self.fdoffset += offset

            if f is not None and f.valid:
                picture, audio, efm = f.downscale(
                    linesout=self.output_lines, final=True, audio=self.analog_audio
                )

                self.audio_offset = f.audio_next_offset

                _ = self.computeMetrics(f, None, verbose=True)
                # if "blackToWhiteRFRatio" in metrics and adjusted == False:
                #     keep = 900 if self.isCLV else 30
                #     self.bw_ratios.append(metrics["blackToWhiteRFRatio"])
                #     self.bw_ratios = self.bw_ratios[-keep:]

                # redo = not self.checkMTF(f, self.prevfield)
                redo = False

                # Perform AGC changes on first fields only to prevent luma mismatch intra-field
                if self.useAGC and f.isFirstField and f.sync_confidence > 80:
                    sync_hz, ire0_hz = self.detectLevels(f)
                    vsync_ire = self.rf.SysParams["vsync_ire"]

                    sync_ire_diff = np.abs(self.rf.hztoire(sync_hz) - vsync_ire)
                    ire0_diff = np.abs(self.rf.hztoire(ire0_hz))

                    acceptable_diff = 2 if self.fields_written else 0.5

                    if max(sync_ire_diff, ire0_diff) > acceptable_diff:
                        redo = True
                        self.rf.AGClevels[0].push(ire0_hz)
                        # Note that vsync_ire is a negative number, so (sync_hz - ire0_hz) is correct
                        self.rf.AGClevels[1].push((sync_hz - ire0_hz) / vsync_ire)

                        self.rf.SysParams["ire0"] = self.rf.AGClevels[0].pull()
                        self.rf.SysParams["hz_ire"] = self.rf.AGClevels[1].pull()

                if adjusted == False and redo == True:
                    self.demodcache.flush_demod()
                    adjusted = True
                    self.fdoffset -= offset
                else:
                    done = True
            else:
                # Probably jumping ahead - delete the previous field so
                # TBC computations aren't thrown off
                if self.curfield is not None and self.badfields is None:
                    self.badfields = (self.curfield, f)
                self.curfield = None

        if f is None or f.valid == False:
            return None

        self.curfield = f

        if f is not None and self.fname_out is not None:
            # Only write a FirstField first
            if len(self.fieldinfo) == 0 and not f.isFirstField:
                return f

            # XXX: this routine currently performs a needed sanity check
            fi, needFiller = self.buildmetadata(f)

            self.lastvalidfield[f.isFirstField] = (f, fi, picture, audio, efm)

            if needFiller:
                if self.lastvalidfield[not f.isFirstField] is not None:
                    self.writeout(self.lastvalidfield[not f.isFirstField])
                    self.writeout(self.lastvalidfield[f.isFirstField])

                # If this is the first field to be written, don't write anything
                return f

            self.writeout(self.lastvalidfield[f.isFirstField])

        return f


class VHSRFDecode(ldd.RFDecode):
    def __init__(
        self,
        inputfreq=40,
        system="NTSC",
        tape_format="VHS",
        rf_options={},
        extra_options={},
    ):

        # First init the rf decoder normally.
        super(VHSRFDecode, self).__init__(
            inputfreq,
            parent_system(system),
            decode_analog_audio=False,
            has_analog_audio=False,
            extra_options=extra_options,
        )

        # No idea if this is a common pythonic way to accomplish it but this gives us values that
        # can't be changed later.
        self.options = namedtuple(
            "Options", "diff_demod_check_value tape_format disable_comb nldeemp"
        )(
            self.iretohz(100) * 2,
            tape_format,
            rf_options.get("disable_comb", False),
            rf_options.get("nldeemp", False),
        )

        # Store a separate setting for *color* system as opposed to 525/625 line here.
        # TODO: Fix upstream so we don't have to fake tell ld-decode code that we are using ntsc for
        # palm to avoid it throwing errors.
        self.color_system = system

        # controls the sharpness EQ gain
        self.sharpness_level = (
            rf_options.get("sharpness", vhs_formats.DEFAULT_SHARPNESS) / 100
        )

        self.dod_threshold_p = rf_options.get(
            "dod_threshold_p", vhs_formats.DEFAULT_THRESHOLD_P_DDD
        )
        self.dod_threshold_a = rf_options.get("dod_threshold_a", None)
        self.dod_hysteresis = rf_options.get(
            "dod_hysteresis", vhs_formats.DEFAULT_HYSTERESIS
        )
        self.chroma_trap = rf_options.get("chroma_trap", False)
        track_phase = rf_options.get("track_phase", None)
        self.recheck_phase = rf_options.get("recheck_phase", False)
        high_boost = rf_options.get("high_boost", None)
        self.notch = rf_options.get("notch", None)
        self.notch_q = rf_options.get("notch_q", 10.0)
        self.disable_diff_demod = rf_options.get("disable_diff_demod", False)
        self.disable_dc_offset = rf_options.get("disable_dc_offset", False)
        self.useAGC = extra_options.get("useAGC", False)
        self.debug = extra_options.get("debug", False)
        self.cafc = rf_options.get("cafc", False)
        # cafc requires --recheck_phase
        self.recheck_phase = True if self.cafc else self.recheck_phase
        self.sync_clip = rf_options.get("sync_clip", False)

        if track_phase is None:
            self.track_phase = 0
            self.detect_track = True
            self.needs_detect = True
        elif track_phase == 0 or track_phase == 1:
            self.track_phase = track_phase
            self.detect_track = False
            self.needs_detect = False
        else:
            raise Exception("Track phase can only be 0, 1 or None")
        self.hsync_tolerance = 0.8

        self.field_number = 0
        self.last_raw_loc = None

        # Then we override the laserdisc parameters with VHS ones.
        if system == "PAL":
            if tape_format == "UMATIC":
                self.SysParams = copy.deepcopy(vhs_formats.SysParams_PAL_UMATIC)
                self.DecoderParams = copy.deepcopy(vhs_formats.RFParams_PAL_UMATIC)
            elif tape_format == "SVHS":
                # Give the decoder it's separate own full copy to be on the safe side.
                self.SysParams = copy.deepcopy(vhs_formats.SysParams_PAL_SVHS)
                self.DecoderParams = copy.deepcopy(vhs_formats.RFParams_PAL_SVHS)
            else:
                # Give the decoder it's separate own full copy to be on the safe side.
                self.SysParams = copy.deepcopy(vhs_formats.SysParams_PAL_VHS)
                self.DecoderParams = copy.deepcopy(vhs_formats.RFParams_PAL_VHS)
        elif system == "NTSC":
            if tape_format == "UMATIC":
                self.SysParams = copy.deepcopy(vhs_formats.SysParams_NTSC_UMATIC)
                self.DecoderParams = copy.deepcopy(vhs_formats.RFParams_NTSC_UMATIC)
            elif tape_format == "SVHS":
                self.SysParams = copy.deepcopy(vhs_formats.SysParams_NTSC_SVHS)
                self.DecoderParams = copy.deepcopy(vhs_formats.RFParams_NTSC_SVHS)
            else:
                self.SysParams = copy.deepcopy(vhs_formats.SysParams_NTSC_VHS)
                self.DecoderParams = copy.deepcopy(vhs_formats.RFParams_NTSC_VHS)
        elif system == "MPAL":
            if tape_format != "VHS":
                ldd.logger.warning(
                    'Tape format "%s" not supported for MPAL yet', tape_format
                )
            self.SysParams = copy.deepcopy(vhs_formats.SysParams_MPAL_VHS)
            self.DecoderParams = copy.deepcopy(vhs_formats.RFParams_MPAL_VHS)
        else:
            raise Exception("Unknown video system! ", system)

        # As agc can alter these sysParams values, store a copy to then
        # initial value for reference.
        self.sysparams_const = namedtuple("Starting_values", "hz_ire vsync_hz")(
            self.SysParams["hz_ire"], self.iretohz(self.SysParams["vsync_ire"])
        )

        # Lastly we re-create the filters with the new parameters.
        self.computevideofilters_b()

        DP = self.DecoderParams

        self.high_boost = high_boost if high_boost is not None else DP["boost_bpf_mult"]

        # Heterodyning / chroma wave related filter part

        self.chromaAFC = ChromaAFC(
            self.freq_hz,
            DP["chroma_bpf_upper"] / DP["color_under_carrier"],
            self.SysParams,
            self.DecoderParams["color_under_carrier"],
            tape_format=tape_format,
        )

        self.Filters["FVideoBurst"] = self.chromaAFC.get_chroma_bandpass()

        if self.notch is not None:
            if not self.cafc:
                self.Filters["FVideoNotch"] = sps.iirnotch(
                    self.notch / self.freq_half, self.notch_q
                )
            else:
                self.Filters["FVideoNotch"] = sps.iirnotch(
                    self.notch / self.chromaAFC.getOutFreqHalf(), self.notch_q
                )

            self.Filters["FVideoNotchF"] = lddu.filtfft(
                self.Filters["FVideoNotch"], self.blocklen
            )
        else:
            self.Filters["FVideoNotch"] = None, None

        # The following filters are for post-TBC:
        # The output sample rate is 4fsc
        self.Filters["FChromaFinal"] = self.chromaAFC.get_chroma_bandpass_final()
        self.Filters["FBurstNarrow"] = self.chromaAFC.get_burst_narrow()
        self.chroma_heterodyne = self.chromaAFC.getChromaHet()
        self.fsc_wave, self.fsc_cos_wave = self.chromaAFC.getFSCWaves()

        # Increase the cutoff at the end of blocks to avoid edge distortion from filters
        # making it through.
        self.blockcut_end = 1024
        self.demods = 0

        if self.sharpness_level != 0:
            # sharpness filter / video EQ
            iir_eq_loband = utils.firdes_highpass(
                self.freq_hz,
                DP["video_eq"]["loband"]["corner"],
                DP["video_eq"]["loband"]["transition"],
                DP["video_eq"]["loband"]["order_limit"],
            )

            self.videoEQFilter = {
                0: utils.FiltersClass(iir_eq_loband[0], iir_eq_loband[1], self.freq_hz),
                # 1: utils.FiltersClass(iir_eq_hiband[0], iir_eq_hiband[1], self.freq_hz),
            }

        self.chromaTrap = ChromaSepClass(self.freq_hz, self.SysParams["fsc_mhz"])

        self.AGClevels = StackableMA(
            window_average=self.SysParams["FPS"] / 2
        ), StackableMA(window_average=self.SysParams["FPS"] / 2)
        self.resync = Resync(self.freq_hz, self.SysParams, debug=self.debug)

    def computevideofilters(self):
        self.Filters = {}
        # Needs to be defined here as it's referenced in constructor.
        self.Filters["F05_offset"] = 32

    def computevideofilters_b(self):
        # Use some shorthand to compact the code.
        SF = self.Filters
        DP = self.DecoderParams

        SF["hilbert"] = lddu.build_hilbert(self.blocklen)

        video_lpf = sps.butter(
            DP["video_lpf_order"], DP["video_lpf_freq"] / self.freq_hz_half, "low"
        )
        # SF["Fvideo_lpf"] = lddu.filtfft(video_lpf, self.blocklen)
        filter_video_lpf = lddu.filtfft(video_lpf, self.blocklen)

        # additional filters:  0.5mhz, used for sync detection.
        # Using an FIR filter here to get a known delay
        F0_5 = sps.firwin(65, [0.5 / self.freq_half], pass_zero=True)
        filter_05 = lddu.filtfft((F0_5, [1.0]), self.blocklen)
        # SF["F05"] = lddu.filtfft((F0_5, [1.0]), self.blocklen)
        # Defined earlier
        # SF["F05_offset"] = 32

        self.Filters["RFVideoRaw"] = lddu.filtfft(
            sps.butter(
                DP["video_bpf_order"],
                [
                    DP["video_bpf_low"] / self.freq_hz_half,
                    DP["video_bpf_high"] / self.freq_hz_half,
                ],
                btype="bandpass",
            ),
            self.blocklen,
        )

        self.Filters["EnvLowPass"] = sps.butter(
            1, [1.0 / self.freq_half], btype="lowpass"
        )

        # Filter for rf before demodulating.
        y_fm = lddu.filtfft(
            sps.butter(
                DP["video_bpf_order"],
                [
                    DP["video_bpf_low"] / self.freq_hz_half,
                    DP["video_bpf_high"] / self.freq_hz_half,
                ],
                btype="bandpass",
            ),
            self.blocklen,
        )

        y_fm_lowpass = lddu.filtfft(
            sps.butter(
                DP["video_lpf_extra_order"],
                [DP["video_lpf_extra"] / self.freq_hz_half],
                btype="lowpass",
            ),
            self.blocklen,
        )

        y_fm_highpass = lddu.filtfft(
            sps.butter(
                DP["video_hpf_extra_order"],
                [DP["video_hpf_extra"] / self.freq_hz_half],
                btype="highpass",
            ),
            self.blocklen,
        )

        self.Filters["RFVideo"] = y_fm * y_fm_lowpass * y_fm_highpass

        self.Filters["RFTop"] = sps.butter(
            1,
            [
                DP["boost_bpf_low"] / self.freq_hz_half,
                DP["boost_bpf_high"] / self.freq_hz_half,
            ],
            btype="bandpass",
            output="sos",
        )

        # Video (luma) main de-emphasis
        db, da = FMDeEmphasisB(self.freq_hz, DP["deemph_gain"], DP["deemph_mid"]).get()
        # Sync de-emphasis
        db05, da05 = FMDeEmphasis(self.freq_hz, tau=DP["deemph_tau"]).get()

        if False:
            import matplotlib.pyplot as plt

            corner_freq = 1 / (math.pi * 2 * DP["deemph_tau"])

            db2, da2 = FMDeEmphasisB(
                self.freq_hz, DP["deemph_gain"], DP["deemph_mid"] + 50000
            ).get()
            db3, da3 = FMDeEmphasisB(
                self.freq_hz, DP["deemph_gain"], DP["deemph_mid"] - 50000
            ).get()
            self.Filters["FVideo2"] = (
                lddu.filtfft((db2, da2), self.blocklen) * self.Filters["Fvideo_lpf"]
            )
            self.Filters["FVideo3"] = (
                lddu.filtfft((db3, da3), self.blocklen) * self.Filters["Fvideo_lpf"]
            )

            fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, sharex=True)

            w1, h1 = sps.freqz(db, da, fs=self.freq_hz)
            w2, h2 = sps.freqz(db2, da2, fs=self.freq_hz)
            w3, h3 = sps.freqz(db3, da3, fs=self.freq_hz)
            # VHS eyeballed freqs.
            test_arr = np.array(
                [
                    [
                        0.04,
                        0.05,
                        0.07,
                        0.1,
                        corner_freq / 1e6,
                        0.2,
                        0.3,
                        0.4,
                        0.5,
                        0.7,
                        1,
                        2,
                        3,
                        4,
                        5,
                    ],
                    [
                        0.4,
                        0.6,
                        1.2,
                        2.2,
                        3,
                        5.25,
                        7.5,
                        9.2,
                        10.5,
                        11.75,
                        12.75,
                        13.5,
                        13.8,
                        13.9,
                        14,
                    ],
                ]
            )
            test_arr[0] *= 1000000.0
            test_arr[1] *= -1
            #            test_arr[0::] *= 1e6

            # ax1.plot((20 * np.log10(self.Filters["Fdeemp"])))
            #        ax1.plot(hilbert, color='#FF0000')
            # ax1.plot(data, color="#00FF00")
            ax1.plot(test_arr[0], test_arr[1], color="#000000")
            ax1.plot(w1, 20 * np.log10(h1))
            ax2.plot(test_arr[0], test_arr[1], color="#000000")
            ax2.plot(w2, 20 * np.log10(h2))
            ax3.plot(test_arr[0], test_arr[1], color="#000000")
            ax3.plot(w3, 20 * np.log10(h3))
            ax4.plot(test_arr[0], test_arr[1])
            ax1.axhline(-3)
            ax2.axhline(-3)
            ax3.axhline(-3)
            ax1.axhline(-7)
            ax2.axhline(-7)
            ax3.axhline(-7)
            ax1.axvline(corner_freq)
            ax2.axvline(corner_freq)
            ax3.axvline(corner_freq)
            plt.show()

        self.Filters["FEnvPost"] = sps.butter(
            1, [700000 / self.freq_hz_half], btype="lowpass", output="sos"
        )

        # self.Filters["Fdeemp"] = lddu.filtfft((db, da), self.blocklen)
        filter_deemp = lddu.filtfft((db, da), self.blocklen)
        # self.Filters["Fdeemp_05"] = lddu.filtfft((db05, da05), self.blocklen)
        self.Filters["FVideo"] = filter_video_lpf * filter_deemp

        SF["FVideo05"] = filter_video_lpf * filter_deemp * filter_05

        # SF["YNRHighPass"] = sps.butter(
        #     1,
        #     [
        #         (0.5e6) / self.freq_hz_half,
        #     ],
        #     btype="highpass",
        #     output="sos",
        # )

        if self.options.nldeemp:
            SF["NLHighPassF"] = lddu.filtfft(
                sps.butter(
                    1,
                    [DP["nonlinear_highpass_freq"] / self.freq_hz_half],
                    btype="highpass",
                ),
                self.blocklen,
            )

    def computedelays(self, mtf_level=0):
        """Override computedelays
        It's normally used for dropout compensation, but the dropout compensation implementation
        in ld-decode assumes composite color. This function is called even if it's disabled, and
        seems to break with the VHS setup, so we disable it by overriding it for now.
        """
        # Set these to 0 for now, the metrics calculations look for them.
        self.delays = {}
        self.delays["video_sync"] = 0
        self.delays["video_white"] = 0

    # It enhances the upper band of the video signal
    def video_EQ(self, demod):
        overlap = 10  # how many samples the edge distortion produces
        ha = self.videoEQFilter[0].filtfilt(demod)
        hb = self.videoEQFilter[0].lfilt(demod[:overlap])
        hc = np.concatenate(
            (hb[:overlap], ha[overlap:])
        )  # edge distortion compensation, needs check
        hf = np.multiply(self.DecoderParams["video_eq"]["loband"]["gain"], hc)

        gain = self.sharpness_level
        result = np.multiply(np.add(np.roll(np.multiply(gain, hf), 0), demod), 1)

        return result

    def demodblock(
        self, data=None, mtf_level=0, fftdata=None, cut=False, thread_benchmark=False
    ):
        rv = {}
        demod_start_time = time.time()
        if fftdata is not None:
            indata_fft = fftdata
        elif data is not None:
            indata_fft = npfft.fft(data[: self.blocklen])
        else:
            raise Exception("demodblock called without raw or FFT data")

        if data is None:
            data = npfft.ifft(indata_fft).real

        if self.notch is not None:
            indata_fft = indata_fft * self.Filters["FVideoNotchF"]

        raw_filtered = npfft.ifft(
            indata_fft * self.Filters["RFVideoRaw"] * self.Filters["hilbert"]
        ).real

        # Calculate an evelope with signal strength using absolute of hilbert transform.
        # Roll this a bit to compensate for filter delay, value eyballed for now.
        raw_env = np.roll(np.abs(raw_filtered), 4)
        # Downconvert to single precision for some possible speedup since we don't need
        # super high accuracy for the dropout detection.
        env = utils.filter_simple(raw_env, self.Filters["FEnvPost"]).astype(np.single)
        env_mean = np.mean(env)

        # Applies RF filters
        indata_fft_filt = indata_fft * self.Filters["RFVideo"]
        data_filtered = npfft.ifft(indata_fft_filt)

        # Boost high frequencies in areas where the signal is weak to reduce missed zero crossings
        # on sharp transitions. Using filtfilt to avoid phase issues.
        if len(np.where(env == 0)[0]) == 0:  # checks for zeroes on env
            high_part = utils.filter_simple(data_filtered, self.Filters["RFTop"]) * (
                (env_mean * 0.9) / env
            )
            indata_fft_filt += npfft.fft(high_part * self.high_boost)
        else:
            ldd.logger.warning("RF signal is weak. Is your deck tracking properly?")

        hilbert = npfft.ifft(indata_fft_filt * self.Filters["hilbert"])

        # FM demodulator
        demod = unwrap_hilbert(hilbert, self.freq_hz).real

        if self.chroma_trap:
            # applies the Subcarrier trap
            demod = self.chromaTrap.work(demod)

        # Disabled if sharpness level is zero (default).
        if self.sharpness_level > 0:
            # applies the video EQ
            demod = self.video_EQ(demod)

        # If there are obviously out of bounds values, do an extra demod on a diffed waveform and
        # replace the spikes with data from the diffed demod.
        if not self.disable_diff_demod:
            check_value = self.options.diff_demod_check_value

            if np.max(demod[20:-20]) > check_value:
                demod_b = unwrap_hilbert(
                    np.pad(np.diff(hilbert), (1, 0), mode="constant"), self.freq_hz
                ).real
                demod = replace_spikes(demod, demod_b, check_value)

        # applies main deemphasis filter
        demod_fft = npfft.rfft(demod)
        out_video_fft = demod_fft * self.Filters["FVideo"][0 : (self.blocklen // 2) + 1]
        out_video = npfft.irfft(out_video_fft).real

        if self.options.nldeemp:
            # Extract the high frequency part of the signal
            hf_part = npfft.irfft(
                out_video_fft
                * self.Filters["NLHighPassF"][0 : (self.blocklen // 2) + 1]
            )
            # Limit it to preserve sharp transitions
            limited_hf_part = np.clip(
                hf_part,
                self.DecoderParams["nonlinear_highpass_limit_l"],
                self.DecoderParams["nonlinear_highpass_limit_h"],
            )
            # And subtract it from the output signal.
            out_video -= limited_hf_part
            # out_video = hf_part + self.iretohz(50)

        out_video05 = npfft.irfft(
            demod_fft * self.Filters["FVideo05"][0 : (self.blocklen // 2) + 1]
        ).real
        out_video05 = np.roll(out_video05, -self.Filters["F05_offset"])

        # Filter out the color-under signal from the raw data.
        out_chroma = (
            demod_chroma_filt(
                data,
                self.Filters["FVideoBurst"],
                self.blocklen,
                self.Filters["FVideoNotch"],
                self.notch,
                # if cafc is enabled, this filtering will be done after TBC
            )
            if not self.cafc
            else data[: self.blocklen]
        )

        if False:
            import matplotlib.pyplot as plt

            fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, sharex=True)

            # ax1.plot((20 * np.log10(self.Filters["Fdeemp"])))
            #        ax1.plot(hilbert, color='#FF0000')
            ax1.plot(data, color="#00FF00")
            ax2.plot(data_filtered, color="#00FF00")
            ax3.plot(demod)
            ax4.plot(out_video, color="#00FF00")
            # ax3.plot(data_filtered)
            # ax3.plot(hilbert_t.real)
            ax4.plot(hilbert.real)
            ax1.axhline(self.iretohz(0))
            ax1.axhline(self.iretohz(self.SysParams["vsync_ire"]))
            # ax1.axhline(self.iretohz(self.SysParams["vsync_ire"] - 5))
            # print("Vsync IRE", self.SysParams["vsync_ire"])
            # ax2 = ax1.twinx()
            # ax3 = ax1.twinx()
            # ax1.plot(
            #     np.arange(self.blocklen) / self.blocklen * self.freq_hz, indata_fft.real
            # )
            # # ax1.plot(env, color="#00FF00")
            # # ax1.axhline(0)
            # # ax1.plot(demod_b, color="#000000")
            # ax2.plot(
            #     np.arange(self.blocklen) / self.blocklen * self.freq_hz,
            #     20 * np.log10(abs(self.Filters["RFVideo"])),
            # )
            # ax2.axhline(0)
            # ax3.plot(
            #     np.arange(self.blocklen) / self.blocklen * self.freq_hz, indata_fft_filt
            # )
            # ax3.plot(np.arange(self.blocklen) / self.blocklen * self.freq_hz, )
            # ax3.axhline(0)
            # ax4.plot(np.pad(np.diff(hilbert), (0, 1), mode="constant"))
            # ax4.axhline(0)
            #            ax3.plot(np.angle(hilbert))
            #            ax4.plot(hilbert.imag)
            #            crossings = find_crossings(env, 700)
            #            ax3.plot(crossings, color="#0000FF")
            plt.show()
            # exit(0)

        # demod_burst is a bit misleading, but keeping the naming for compatability.
        video_out = np.rec.array(
            [out_video, demod, out_video05, out_chroma, env, data],
            names=["demod", "demod_raw", "demod_05", "demod_burst", "envelope", "raw"],
        )

        rv["video"] = (
            video_out[self.blockcut : -self.blockcut_end] if cut else video_out
        )

        demod_end_time = time.time()
        if thread_benchmark:
            ldd.logger.debug(
                "Demod thread %d, work done in %.02f msec"
                % (os.getpid(), (demod_end_time - demod_start_time) * 1e3)
            )

        return rv
