""" Primary module for guiding the reduction of long/multi-slit data
"""
from __future__ import (print_function, absolute_import, division, unicode_literals)

import yaml
import numpy as np

from astropy import units

from pypit import msgs
from pypit import arparse as settings
from pypit import arflux
from pypit import arload
from pypit import armasters
from pypit import armbase
from pypit import arproc
from pypit.core import arprocimg
from pypit import arsave
from pypit import arsciexp
from pypit.core import arsetup
from pypit import arpixels
from pypit.core import arsort
from pypit import wavetilts
from pypit import artrace
from pypit import arcimage
from pypit import bpmimage
from pypit import biasframe
from pypit import traceslits
from pypit import traceimage

from pypit import ardebug as debugger


def ARMS(fitstbl, setup_dict, reuseMaster=False, reloadMaster=True, sciexp=None):
    """
    Automatic Reduction of Multislit Data

    Parameters
    ----------
    fitsdict : dict
      Contains relevant information from fits header files
    reuseMaster : bool
      If True, a master frame that will be used for another science frame
      will not be regenerated after it is first made.
      This setting comes with a price, and if a large number of science frames are
      being generated, it may be more efficient to simply regenerate the master
      calibrations on the fly.

    Returns
    -------
    status : int
      Status of the reduction procedure
      0 = Successful full execution
      1 = Successful processing of setup or calcheck
    """
    debugger.set_trace()  # MOVE ARMLSD ON TOP OF THIS YOU FOOL!
    status = 0

    # Generate sciexp list, if need be (it will be soon)
    if sciexp is None:
        sciexp = []
        all_sci_ID = fitstbl['sci_ID'].data[fitstbl['science']]  # Binary system: 1,2,4,8, etc.
        for ii in all_sci_ID:
            sciexp.append(arsciexp.ScienceExposure(ii, fitstbl, settings.argflag,
                                                   settings.spect, do_qa=True))
    numsci = len(sciexp)

    # Init calib dict
    calib_dict = {}

    # Loop on science exposure
    for sc in range(numsci):

        slf = sciexp[sc]
        sci_ID = slf.sci_ID
        scidx = slf._idx_sci[0]
        msgs.info("Reducing file {0:s}, target {1:s}".format(fitstbl['filename'][scidx], slf._target_name))
        msgs.sciexp = slf  # For QA writing on exit, if nothing else.  Could write Masters too

        #if reloadMaster and (sc > 0):
        #    settings.argflag['reduce']['masters']['reuse'] = True

        # Loop on Detectors
        for kk in range(settings.spect['mosaic']['ndet']):
            det = kk + 1  # Detectors indexed from 1
            if settings.argflag['reduce']['detnum'] is not None:
                if det not in map(int, settings.argflag['reduce']['detnum']):
                    msgs.warn("Skipping detector {:d}".format(det))
                    continue
                else:
                    msgs.warn("Restricting the reduction to detector {:d}".format(det))

            slf.det = det
            dnum = settings.get_dnum(det)
            msgs.info("Working on detector {:s}".format(dnum))

            # Setup
            namp = settings.spect[dnum]["numamplifiers"]
            setup = arsetup.instr_setup(sci_ID, det, fitstbl, setup_dict, namp, must_exist=True)
            settings.argflag['reduce']['masters']['setup'] = setup
            slf.setup = setup

            ###############
            # Get data sections (Could avoid doing this for every sciexp, but it is quick)
            datasec_img = arprocimg.get_datasec_trimmed(fitstbl, det, scidx, settings.argflag, settings.spect)
            #slf._datasec[det-1] = pix_to_amp(naxis0, naxis1, datasec, numamplifiers)

            # Calib dict
            if setup not in calib_dict.keys():
                calib_dict[setup] = {}

            # TODO -- Update/avoid the following with new settings
            tsettings = settings.argflag.copy()
            tsettings['detector'] = settings.spect[settings.get_dnum(det)]
            try:
                tsettings['detector']['dataext'] = tsettings['detector']['dataext01']  # Kludge; goofy named key
            except KeyError: # LRIS, DEIMOS
                tsettings['detector']['dataext'] = None
            tsettings['detector']['dispaxis'] = settings.argflag['trace']['dispersion']['direction']

            ###############
            # Prepare for Bias subtraction
            #   bias will either be an image (ndarray) or a command (str, e.g. 'overscan') or none
            if 'bias' in calib_dict[setup].keys():
                msbias = calib_dict[setup]['bias']
            else:
                # Init
                bias = biasframe.BiasFrame(settings=tsettings, setup=setup, det=det, fitstbl=fitstbl, sci_ID=sci_ID)
                # Grab/build the MasterFrame (ndarray or str)
                #   If an image is generated, it will be saved to disk a a MasterFrame
                msbias = bias.master()
                # Save
                calib_dict[setup]['bias'] = msbias

            ###############
            # Generate a master arc frame
            if 'arc' in calib_dict[setup].keys():
                msarc = calib_dict[setup]['arc']
            else:
                AImage = arcimage.ArcImage([], spectrograph=settings.argflag['run']['spectrograph'],
                                           settings=tsettings, det=det, setup=setup, sci_ID=sci_ID,
                                           msbias=msbias, fitstbl=fitstbl)
                msarc = AImage.master()
                # Save
                calib_dict[setup]['arc'] = msarc

            ###############
            # Generate a bad pixel mask (should not repeat)
            if 'bpm' in calib_dict[setup].keys():
                msbpm = calib_dict[setup]['bpm']
            else:
                bpmImage = bpmimage.BPMImage(spectrograph=settings.argflag['run']['spectrograph'],
                                             settings=tsettings, det=det,
                                             shape=msarc.shape,
                                             binning=fitstbl['binning'][scidx],
                                             reduce_badpix=settings.argflag['reduce']['badpix'],
                                             msbias=msbias)
                msbpm = bpmImage.build()
                # Save
                calib_dict[setup]['bpm'] = msbpm

            ###############
            # Generate an array that provides the physical pixel locations on the detector
            pixlocn = arpixels.gen_pixloc(msarc.shape, det, settings.argflag)
            # TODO -- Deprecate using slf for this
            slf.SetFrame(slf._pixlocn, pixlocn, det)

            ###############
            # Slit Tracing
            if 'trace' in calib_dict[setup].keys():  # Internal
                Tslits = calib_dict[setup]['trace']
            else:
                # Load master frame?
                if (settings.argflag['reduce']['masters']['reuse']) or (settings.argflag['reduce']['masters']['force']):
                    mstrace_name = armasters.master_name('trace', setup)
                    Tslits = traceslits.TraceSlits.from_master_files(mstrace_name, load_pix_obj=True)  # Returns None if none exists
                else:
                    Tslits = None

                # Build it?  Beginning with the trace image
                if Tslits is None:
                    # Build the trace image first
                    trace_image_files = arsort.list_of_files(fitstbl, 'trace', sci_ID)
                    Timage = traceimage.TraceImage(trace_image_files,
                                                        spectrograph=settings.argflag['run']['spectrograph'],
                                                        settings=tsettings, det=det)
                    mstrace = Timage.process(bias_subtract=msbias, trim=settings.argflag['reduce']['trim'])

                    # Setup up the settings (will be Refactored with settings)
                    tmp = dict(trace=settings.argflag['trace'], masters=settings.argflag['reduce']['masters'])
                    tmp['masters']['directory'] = settings.argflag['run']['directory']['master']+'_'+ settings.argflag['run']['spectrograph']

                    # Now trace me some slits
                    Tslits = traceslits.TraceSlits(mstrace, slf._pixlocn[det-1], det=det, settings=tmp,
                                                   binbpx=msbpm, setup=setup)
                    _ = Tslits.run(armlsd=True)
                    # QA
                    Tslits._qa()
                    # Save to disk
                    Tslits.save_master()
                # Save in calib
                calib_dict[setup]['trace'] = Tslits

            # Save in slf
            # TODO -- Deprecate this means of holding the info (e.g. just pass around Tslits)
            slf.SetFrame(slf._lordloc, Tslits.lcen, det)
            slf.SetFrame(slf._rordloc, Tslits.rcen, det)
            slf.SetFrame(slf._pixcen, Tslits.pixcen, det)
            slf.SetFrame(slf._pixwid, Tslits.pixwid, det)
            slf.SetFrame(slf._lordpix, Tslits.lordpix, det)
            slf.SetFrame(slf._rordpix, Tslits.rordpix, det)
            slf.SetFrame(slf._slitpix, Tslits.slitpix, det)

            # Initialize maskslit
            slf._maskslits[det-1] = np.zeros(slf._lordloc[det-1].shape[1], dtype=bool)

            ###############
            # Generate the 1D wavelength solution
            update = slf.MasterWaveCalib(fitstbl, det, msarc)
            if update and reuseMaster:
                armbase.UpdateMasters(sciexp, sc, det, ftype="arc", chktype="trace")

            ###############
            # Derive the spectral tilt
            if 'tilts' in calib_dict[setup].keys():
                mstilts = calib_dict[setup]['tilts']
                wt_maskslits = calib_dict[setup]['wtmask']
            else:
                # Settings kludges
                tilt_settings = dict(tilts=settings.argflag['trace']['slits']['tilts'].copy())
                tilt_settings['tilts']['function'] = settings.argflag['trace']['slits']['function']
                tilt_settings['masters'] = settings.argflag['reduce']['masters']
                tilt_settings['masters']['directory'] = settings.argflag['run']['directory']['master']+'_'+ settings.argflag['run']['spectrograph']
                settings_det = {}
                settings_det[dnum] = settings.spect[dnum].copy()
                # Instantiate
                wTilt = wavetilts.WaveTilts(msarc, settings=tilt_settings, det=det, setup=setup,
                                            lordloc=Tslits.lcen, rordloc=Tslits.rcen,
                                            pixlocn=Tslits.pixlocn, pixcen=Tslits.pixcen,
                                            slitpix=Tslits.slitpix, settings_det=settings_det)
                # Master
                mstilts = wTilt.master()
                if mstilts is None:
                    mstilts, wt_maskslits = wTilt.run(maskslits=slf._maskslits[det-1])
                    wTilt.save_master()
                else:
                    wt_maskslits = np.zeros(len(slf._maskslits[det-1]), dtype=bool)
                # Save
                calib_dict[setup]['tilts'] = mstilts
                calib_dict[setup]['wtmask'] = wt_maskslits
            slf._maskslits[det-1] += wt_maskslits

            ###############
            # Prepare the pixel flat field frame
            if settings.argflag['reduce']['flatfield']['perform']:  # Only do it if the user wants to flat field


            update = slf.MasterFlatField(fitstbl, det, msbias, datasec_img, mstilts)
            if update and reuseMaster: armbase.UpdateMasters(sciexp, sc, det, ftype="flat", chktype="pixelflat")

            ###############
            # Generate/load a master wave frame
            update = slf.MasterWave(det, wv_calib, mstilts)
            if update and reuseMaster:
                armbase.UpdateMasters(sciexp, sc, det, ftype="arc", chktype="wave")

            ###############
            # Check if the user only wants to prepare the calibrations only
            #msgs.info("All calibration frames have been prepared")
            #if settings.argflag['run']['preponly']:
            #    msgs.info("If you would like to continue with the reduction, disable the command:" + msgs.newline() +
            #              "run preponly False")
            #    continue

            ###############
            # Write setup
            #setup = arsort.calib_setup(sc, det, fitsdict, setup_dict, write=True)
            # Write MasterFrames (currently per detector)
            #armasters.save_masters(slf, det, setup)

            ###############
            # Load the science frame and from this generate a Poisson error frame
            msgs.info("Loading science frame")
            sciframe = arload.load_frames(fitstbl, [scidx], det,
                                          frametype='science',
                                          msbias=msbias) # slf._msbias[det-1])
            sciframe = sciframe[:, :, 0]
            # Extract
            msgs.info("Processing science frame")
            arproc.reduce_multislit(slf, mstilts, sciframe, msbpm, datasec_img, scidx, fitstbl, det)

            ######################################################
            # Reduce standard here; only legit todo if the mask is the same
            std_idx = arsort.ftype_indices(fitstbl, 'standard', sci_ID)
            if len(std_idx) > 0:
                std_idx = std_idx[0]
            else:
                continue
            stdslf = std_dict[std_idx]
            if stdslf.extracted is False:
                # Fill up the necessary pieces
                for iattr in ['pixlocn', 'lordloc', 'rordloc', 'pixcen', 'pixwid', 'lordpix', 'rordpix',
                              'slitpix', 'satmask', 'maskslits', 'slitprof',
                              'mspixelflatnrm', 'mswave']:
                    setattr(stdslf, '_'+iattr, getattr(slf, '_'+iattr))  # Brings along all the detectors, but that is ok
                # Load
                stdframe = arload.load_frames(fitstbl, [std_idx], det, frametype='standard', msbias=msbias)
                stdframe = stdframe[:, :, 0]
                # Reduce
                msgs.info("Processing standard frame")
                arproc.reduce_multislit(stdslf, mstilts, stdframe, msbpm, datasec_img, std_idx, fitstbl, det, standard=True)
                # Finish
                stdslf.extracted = True


        # TODO -- When we refactor ScienceExposure, something will need to carry all the individual images until we write them out..
        # Write 1D spectra
        save_format = 'fits'
        if save_format == 'fits':
            arsave.save_1d_spectra_fits(slf, fitstbl)
        elif save_format == 'hdf5':
            arsave.save_1d_spectra_hdf5(slf)
        else:
            msgs.error(save_format + ' is not a recognized output format!')
        arsave.save_obj_info(slf, fitstbl)
        # Write 2D images for the Science Frame
        arsave.save_2d_images(slf, fitstbl)
        # Free up some memory by replacing the reduced ScienceExposure class
        sciexp[sc] = None

    #########################
    # Flux at the very end..
    #########################
    if (settings.argflag['reduce']['calibrate']['flux'] == True) and False:
        # Standard star (is this a calibration, e.g. goes above?)
        msgs.info("Processing standard star")
        msgs.info("Assuming one star per detector mosaic")
        msgs.info("Waited until last detector to process")

        if (settings.argflag['reduce']['calibrate']['sensfunc']['archival'] == 'None'):
            update = slf.MasterStandard(fitstbl)
            if update and reuseMaster:
                armbase.UpdateMasters(sciexp, sc, 0, ftype="standard")
        else:
            sensfunc = yaml.load(open(settings.argflag['reduce']['calibrate']['sensfunc']['archival']))
            # Yaml does not do quantities, so make the sensfunc min/max wave quantities
            sensfunc['wave_max'] *= units.angstrom
            sensfunc['wave_min'] *= units.angstrom
            slf.SetMasterFrame(sensfunc, "sensfunc", None, mkcopy=False)
            msgs.info(
                "Using archival sensfunc {:s}".format(settings.argflag['reduce']['calibrate']['sensfunc']['archival']))

        msgs.info("Fluxing with {:s}".format(slf._sensfunc['std']['name']))
        for kk in range(settings.spect['mosaic']['ndet']):
            det = kk + 1  # Detectors indexed from 1
            if slf._specobjs[det - 1] is not None:
                arflux.apply_sensfunc(slf, det, scidx, fitstbl)
            else:
                msgs.info("There are no objects on detector {0:d} to apply a flux calibration".format(det))

    return status
