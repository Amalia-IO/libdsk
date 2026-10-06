/***************************************************************************
 *                                                                         *
 *    LIBDSK: General floppy and diskimage access library                  *
 *    Copyright (C) 2001-2, 2016-7 John Elliott <seasip.webmaster@gmail.com>* 
 *                                                                         *
 *    This library is free software; you can redistribute it and/or        *
 *    modify it under the terms of the GNU Library General Public          *
 *    License as published by the Free Software Foundation; either         *
 *    version 2 of the License, or (at your option) any later version.     *
 *                                                                         *
 *    This library is distributed in the hope that it will be useful,      *
 *    but WITHOUT ANY WARRANTY; without even the implied warranty of       *
 *    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU    *
 *    Library General Public License for more details.                     *
 *                                                                         *
 *    You should have received a copy of the GNU Library General Public    *
 *    License along with this library; if not, write to the Free           *
 *    Software Foundation, Inc., 59 Temple Place - Suite 330, Boston,      *
 *    MA 02111-1307, USA                                                   *
 *                                                                         *
 ***************************************************************************/

#define CP2_USER_BLOCK "ucp2"
/* For some reason sector offset is biased by 0x16ad (original address of 
 * buffer in data segment? */
#define CP2_BIAS 0x16AD

#define CP2_SEGHBUF_LEN 0x1230	/* Maximum length of sector headers */
#define CP2_SEGDBUF_LEN 0xD802	/* Maximum length of sector data */

/* This driver works with the "CP2" disk image format, created by 
 * "Snatch-it" (a hack of the disk copier COPYIIPC that saves disk 
 * images to a file.
 *
 * Write support is entirely experimental; it produces a file that can be
 * parsed by this driver, but there are some fields I don't know the 
 * meaning of and those are left unpopulated. Also, the order of sectors
 * in the file as written differs from the order of sectors in the 
 * original file.
 *
 * The CP2 file format doesn't seem to be documented much online but PCE 
 * has some code that parses it in psi-img-cp2.c, which implies:
 * 
 * 0000: Header (30 bytes) comprising:
 *       0000 "SOFTWARE PIRATES" - fixed text
 *       0010 "Release " - fixed text
 *       0018 "x.yy"     - Version number, usually ASCII but PCE also checks 
 *                         for "6.0\0x0a"
 *       001C "$"        - End of version number 
 *       001D "0"        - ASCII volume number
 * 001E: File data. File consists of a number of segments <= 64k, each formed:
 *       DW	size	 - length of headers (should be a multiple of 387, +1)
 *       Track headers. A track header begins:
 *	  	DB cylinder
 *	        DB head
 *              DB sector_count
 *              DS 384	;Space for sector headers
 *              There is space for up to 24 sector headers per track, each 
 *              of which is 16 bytes:
 *                  DB read_result
 *		    DB ST0
 *		    DB ST1
 *		    DB ST2
 *		    DB id_cylinder
 *		    DB id_head
 *		    DB id_sector
 *		    DB id_size
 *		    DW offset   -- offset to sector data within segment
 *		    DW unknown
 *		    DW unknown
 *		    DW unknown
 *
 */

#include <stdio.h>
#include "libdsk.h"
#include "drvi.h"
#include "drvldbs.h"
#include "drvcp2.h"

/* This struct contains function pointers to the driver's functions, and the
 * size of its DSK_DRIVER subclass */

DRV_CLASS dc_cp2 = 
{
	sizeof(CP2_DSK_DRIVER),
	&dc_ldbsdisk,	/* superclass */
	"cp2\0CP2\0",
	"SNATCH-IT CP2 disk image file",
	cp2_open,	/* open */
	cp2_creat,	/* create new */
	cp2_close,	/* close */
};


static dsk_err_t cp2_read_segment(CP2_DSK_DRIVER *cp2self, FILE *fp, DSK_REPORTFUNC diagfunc)
{
	char secid[4];
	unsigned char seglen[2];
	unsigned char seghead[387];
	unsigned char secbuf[4096];
	long offset, hdrlen, datalen, secoff;
	int n, n1, t, x, allsame;
	LDBS_TRACKHEAD *trkh;
	dsk_err_t err;

	offset = ftell(fp);
	if (fread(seglen, 1, sizeof(seglen), fp) < 2)
	{
		return DSK_ERR_OVERRUN;	/* Use to indicate EOF */
	}
	hdrlen = ldbs_peek2(seglen);
	if (hdrlen == 0) 
	{
		diaghead(diagfunc, "Segment header");
		diaghex (diagfunc, offset, seglen, 2, "Header length 0x%04x (End of file)", hdrlen);
		return DSK_ERR_OVERRUN;	/* Use to indicate EOF */
	}
	diaghead(diagfunc, "Segment header");
	diaghex (diagfunc, offset, seglen, 2, "Header length 0x%04x", hdrlen);
/* The segment header consists of a 3-byte track header and 24 16-byte sector
 * headers */
	for (t = 0; t < (hdrlen / 387); t++)
	{
		fseek(fp, offset + 2 + 387 * t, SEEK_SET);
		if (fread(seghead, 1, sizeof(seghead), fp) < sizeof(seghead))
		{
			return DSK_ERR_CORRUPT;
		}
		diaghead(diagfunc, "Track header %d", t + 1);
		diaghex(diagfunc, offset + 387*t + 2, seghead, 1, "Cylinder");
		diaghex(diagfunc, offset + 387*t + 3, seghead+1, 1, "Head");
		diaghex(diagfunc, offset + 387*t + 4, seghead+2, 1, "Sector count");

		trkh = ldbs_trackhead_alloc(seghead[2]);
		if (!trkh) return DSK_ERR_NOMEM;

		if (trkh->count < 9) 		trkh->gap3 = 0x50;
		else if (trkh->count < 10)	trkh->gap3 = 0x52;
		else				trkh->gap3 = 0x17;
		trkh->filler = 0xE5;

		for (n = 0; n < 24; n++)
		{
			if (n < seghead[2])
			{
				trkh->sector[n].st1     = seghead[16 * n  + 5];
				trkh->sector[n].st2     = seghead[16 * n  + 6];
				trkh->sector[n].id_cyl  = seghead[16 * n  + 7];
				trkh->sector[n].id_head = seghead[16 * n  + 8];
				trkh->sector[n].id_sec  = seghead[16 * n  + 9];
				trkh->sector[n].id_psh  = seghead[16 * n  + 10];
				trkh->sector[n].datalen = 128 << seghead[16 * n  + 10];
				secoff = ldbs_peek2(seghead + 16 * n + 11);
				/* According to psi-img-cp2.c, sector sizes < 256 and
				 * > 4096 are not stored. */
				if (secoff < CP2_BIAS || 
				    trkh->sector[n].datalen < 256 || 
				    trkh->sector[n].datalen > 4096)
				{
					trkh->sector[n].copies = 0;
				}
				else
				{
					secoff -= CP2_BIAS;
					trkh->sector[n].copies = 1;
					if (fseek(fp, secoff + hdrlen + offset + 4, SEEK_SET) ||
					    fread(secbuf, 1, trkh->sector[n].datalen, fp) < trkh->sector[n].datalen)
					{
						return DSK_ERR_SYSERR;
					}
					allsame = 1;
					for (x = 1; x < trkh->sector[n].datalen; x++)
					{
						if (secbuf[x] != secbuf[0]) 
						{ 
							allsame = 0; 
							break;
						}
					}
					if (allsame)
					{
						trkh->sector[n].copies = 0;
						trkh->sector[n].filler = secbuf[0];
						trkh->sector[n].blockid = LDBLOCKID_NULL;
					}
					else
					{
						trkh->sector[n].copies = 1;
						trkh->sector[n].filler = 0xF6;
						ldbs_encode_secid(secid, seghead[0], seghead[1], 
							trkh->sector[n].id_sec);
						err = ldbs_putblock(cp2self->cp2_super.ld_store,
							&trkh->sector[n].blockid, secid,
							secbuf, 
							trkh->sector[n].datalen);
						if (err)
						{
							return err;
						}
					}
/*				printf("\nsecoff(%d)=%04x -> %08x\n",
						n, secoff, secoff + hdrlen + offset + 4); */

				}
				err = ldbs_put_trackhead(cp2self->cp2_super.ld_store,
					trkh, seghead[0], seghead[1]);
				if (err) 
				{
					return err;
				}
			}

			diaghead(diagfunc, "Sector header [%d] %d", t + 1, n + 1);
			diaghex(diagfunc, offset + 387*t + 5 + 16 * n, 
				seghead + 3 + 16 * n, 1, "Read result");
			diaghex(diagfunc, offset + 387*t + 6 + 16 * n, 
				seghead + 4 + 16 * n, 1, "ST0");
			diaghex(diagfunc, offset + 387*t + 7 + 16 * n, 
				seghead + 5 + 16 * n, 1, "ST1");
			diaghex(diagfunc, offset + 387*t + 8 + 16 * n, 
				seghead + 6 + 16 * n, 1, "ST2");
			diaghex(diagfunc, offset + 387*t + 9 + 16 * n, 
				seghead + 7 + 16 * n, 1, "Cylinder");
			diaghex(diagfunc, offset + 387*t +10 + 16 * n, 
				seghead + 8 + 16 * n, 1, "Head");
			diaghex(diagfunc, offset + 387*t +11 + 16 * n, 
				seghead + 9 + 16 * n, 1, "Sector");
			diaghex(diagfunc, offset + 387*t +12 + 16 * n, 
				seghead +10 + 16 * n, 1, "Size");
			diaghex(diagfunc, offset + 387*t +13 + 16 * n, 
				seghead +11 + 16 * n, 2, "Offset");
			diaghex(diagfunc, offset + 387*t +15 + 16 * n, 
				seghead +13 + 16 * n, 2, "--?-?-");
			diaghex(diagfunc, offset + 387*t +17 + 16 * n, 
				seghead +15 + 16 * n, 2, "--?-?-");
			diaghex(diagfunc, offset + 387*t +19 + 16 * n, 
				seghead +17 + 16 * n, 2, "--?-?-");
		}
	}
	n1 = (hdrlen / 387) * 387;
	diagrawrange(diagfunc, fp, offset + n1 + 2, hdrlen - n1, "Unknown");

	fseek(fp, offset + hdrlen + 2, SEEK_SET);
	if (fread(seglen, 1, sizeof(seglen), fp) < 2)
	{
		return DSK_ERR_OVERRUN;	/* Use to indicate EOF */
	}
	datalen = ldbs_peek2(seglen);
	diaghead(diagfunc, "Sector data");
	diaghex (diagfunc, offset + hdrlen + 2, seglen, 2, "Sector data length 0x%04x", datalen);
	diagrawrange(diagfunc, fp, offset + hdrlen + 4, datalen, "Sector data");

	fseek(fp, offset + hdrlen + datalen + 4, SEEK_SET);
	return DSK_ERR_OK;
}

dsk_err_t cp2_open(DSK_DRIVER *self, const char *filename, DSK_REPORTFUNC diagfunc)
{
	FILE *fp;
	CP2_DSK_DRIVER *cp2self;
	dsk_err_t err;	
	int n;
	unsigned char buf[32];

	/* Sanity check: Is this meant for our driver? */
	if (self->dr_class != &dc_cp2) return DSK_ERR_BADPTR;
	cp2self = (CP2_DSK_DRIVER *)self;

	fp = fopen(filename, "r+b");
	if (!fp) 
	{
		cp2self->cp2_super.ld_readonly = 1;
		fp = fopen(filename, "rb");
	}
	if (!fp) return DSK_ERR_NOTME;

	/* Try to check the magic number */
	if (fread(buf, 1, 30, fp) < 30 ||
	    memcmp(buf, "SOFTWARE PIRATESRelease ", 24))
	{
		fclose(fp);
		return DSK_ERR_NOTME;
	}
	for (n = 0; n < 30; n++)
	{
		if (buf[n] == '$')
		{
			break;
		}
	}
	if (n >= 30)	/* No terminating $ at end of release string */
	{
		fclose(fp);
		return DSK_ERR_NOTME;
	}
	diaghead(diagfunc, "SNATCH-IT CP2 disk image");
	diaghex(diagfunc,  0,    buf,      16, "File signature");
	diaghex(diagfunc, 16,    buf + 16, n - 15, "Release number");
	diaghex(diagfunc, n + 1, buf +  n + 1, 1, "Volume number");
	
	err = ldbs_new(&cp2self->cp2_super.ld_store, NULL, LDBS_DSK_TYPE);
	if (err)
	{
		fclose(fp);
		return err;
	}
	/* Keep a copy of the filename; when writing back, we will need it */
	cp2self->cp2_filename = dsk_malloc_string(filename);
	if (!cp2self->cp2_filename) 
	{
		fclose(fp);
		ldbs_close(&cp2self->cp2_super.ld_store);
		return DSK_ERR_NOMEM;
	}

        /* Save the release number */
        err = ldbs_putblock_d(cp2self->cp2_super.ld_store, CP2_USER_BLOCK,
                                &buf[0x18], 4);

	if (err)
	{
		fclose(fp);
		ldbs_close(&cp2self->cp2_super.ld_store);
		return err;
	}
	do
	{
		err = cp2_read_segment(cp2self, fp, diagfunc);
	} while (err == DSK_ERR_OK);

	if (err != DSK_ERR_OK && err != DSK_ERR_OVERRUN)
	{
		fclose(fp);
		ldbs_close(&cp2self->cp2_super.ld_store);
		return err;
	}

	dsk_report_end();
	return ldbsdisk_attach(self);
}

/* Default to release 3.02, the earliest release recognised by PCE */
static void cp2_make_header(unsigned char *header)
{
	memset(header, 0, 30);
	strcpy((char *)header, "SOFTWARE PIRATESRelease 3.02$0");
}


dsk_err_t cp2_creat(DSK_DRIVER *self, const char *filename)
{
	CP2_DSK_DRIVER *cp2self;
	FILE *fp;
	dsk_err_t err;
	unsigned char dummy_header[32];
	
	/* Sanity check: Is this meant for our driver? */
	if (self->dr_class != &dc_cp2) return DSK_ERR_BADPTR;
	cp2self = (CP2_DSK_DRIVER *)self;

	/* See if the file can be created. But don't hold it open. */
	fp = fopen(filename, "wb");
	if (!fp) return DSK_ERR_SYSERR;

	cp2_make_header(dummy_header);
	if (fwrite(dummy_header, 1, 30, fp) < 30) 
	{
		fclose(fp);
		return DSK_ERR_SYSERR;
	}
	fclose(fp);

	err = ldbs_new(&cp2self->cp2_super.ld_store, NULL, LDBS_DSK_TYPE);
	if (err) return err;

	/* Keep a copy of the filename, for writing back */
	cp2self->cp2_filename = dsk_malloc_string(filename);
	if (!cp2self->cp2_filename) return DSK_ERR_NOMEM;
	
	return ldbsdisk_attach(self);
}

/* See how much data this track will require */
static size_t cp2_tracksize(LDBS_TRACKHEAD *th)
{
	size_t result = 0;
	int n;
	int count;
	size_t datalen;

	count = th->count; 
	if (count > 24) count = 24;

	for (n = 0; n < count; n++)
	{
		datalen = th->sector[n].datalen;
		if (datalen < 256 || datalen > 4096) datalen = 0;
		result += datalen;
	}
	return result;
}


static dsk_err_t cp2_flush(CP2_DSK_DRIVER *cp2self)
{
	unsigned char ch = 0;
	unsigned n;

	if (cp2self->cp2_segh_cur <=2 && cp2self->cp2_segd_cur <= 2)	/* Nothing to flush */
	{
		return DSK_ERR_OK;
	}
	/* Write an extra byte (checksum?) after the headers */
	for (n = 2; n < cp2self->cp2_segh_cur; n++)
	{
		ch += cp2self->cp2_seghbuf[n];
	}
	cp2self->cp2_seghbuf[cp2self->cp2_segh_cur++] = ch;

	/* Write buffer lengths at the start of the buffers */
	cp2self->cp2_seghbuf[0] = ((cp2self->cp2_segh_cur - 2) & 0xFF);
	cp2self->cp2_seghbuf[1] = ((cp2self->cp2_segh_cur - 2) >> 8);
	cp2self->cp2_segdbuf[0] = ((cp2self->cp2_segd_cur - 2) & 0xFF);
	cp2self->cp2_segdbuf[1] = ((cp2self->cp2_segd_cur - 2) >> 8);

	if (fwrite(cp2self->cp2_seghbuf, 1, cp2self->cp2_segh_cur, 
			cp2self->cp2_fp) < cp2self->cp2_segh_cur ||
	    fwrite(cp2self->cp2_segdbuf, 1, cp2self->cp2_segd_cur, 
			cp2self->cp2_fp) < cp2self->cp2_segd_cur)
		return DSK_ERR_SYSERR;
	/* Buffers flushed; reset counters */
	cp2self->cp2_segh_cur = 2;		/* Write pointers at start of buffers */
	cp2self->cp2_segd_cur = 2;
	memset(cp2self->cp2_seghbuf, 0, CP2_SEGHBUF_LEN);
	memset(cp2self->cp2_segdbuf, 0, CP2_SEGDBUF_LEN);
	return DSK_ERR_OK;
}

static dsk_err_t cp2_save_track(PLDBS ldbs, dsk_pcyl_t cyl, dsk_phead_t head,
				LDBS_TRACKHEAD *th, void *param)
{
	CP2_DSK_DRIVER *cp2self = (CP2_DSK_DRIVER *)param;
	unsigned char *trkh, *sech, *secbuf;
	unsigned seco;
	int n;
	size_t datalen, buflen;
	dsk_err_t err;

	buflen = cp2_tracksize(th);
	if (cp2self->cp2_segh_cur + 387 > CP2_SEGHBUF_LEN ||
	    cp2self->cp2_segd_cur + buflen > CP2_SEGDBUF_LEN)
	{
		err = cp2_flush(cp2self);
		if (err) return err;
	}
	trkh = cp2self->cp2_seghbuf + cp2self->cp2_segh_cur;
	sech = trkh + 3;
	secbuf = cp2self->cp2_segdbuf + cp2self->cp2_segd_cur;

	trkh[0] = cyl;
	trkh[1] = head;
	trkh[2] = (unsigned char)(th->count); 
	if (trkh[2] > 24) trkh[2] = 24;

	for (n = 0; n < trkh[2]; n++)
	{
		sech[n * 16 + 1] = th->sector[n].st1;
		sech[n * 16 + 2] = th->sector[n].st2;
		sech[n * 16 + 4] = th->sector[n].id_cyl;
		sech[n * 16 + 5] = th->sector[n].id_head;
		sech[n * 16 + 6] = th->sector[n].id_sec;
		sech[n * 16 + 7] = th->sector[n].id_psh;

		seco = cp2self->cp2_segd_cur + CP2_BIAS - 2;

		sech[n * 16 + 8] = (seco & 0xFF);
		sech[n * 16 + 9] = (seco >> 8);

		datalen = th->sector[n].datalen;
		if (datalen < 256 || datalen > 4096) datalen = 0;

		buflen = datalen;
		if (th->sector[n].blockid && datalen)
		{
			err = ldbs_getblock(ldbs, th->sector[n].blockid, NULL,
					secbuf, &buflen);
			if (err) return err;
		}
		else if (datalen)
		{
			memset(secbuf, th->sector[n].filler, datalen);
		}
		secbuf += datalen;
		cp2self->cp2_segd_cur += datalen;
	}
	cp2self->cp2_segh_cur += 387;
	return DSK_ERR_OK;
}


dsk_err_t cp2_close(DSK_DRIVER *self)
{
	CP2_DSK_DRIVER *cp2self;
	dsk_err_t err = DSK_ERR_OK;
	unsigned char header[32];
	size_t len;

	if (self->dr_class != &dc_cp2) return DSK_ERR_BADPTR;
	cp2self = (CP2_DSK_DRIVER *)self;

	/* Firstly, ensure any pending changes are flushed to the LDBS 
	 * blockstore. Once this has been done we own the blockstore again 
	 * and have to close it after we've finished with it. */
	err = ldbsdisk_detach(self); 
	if (err)
	{
		dsk_free(cp2self->cp2_filename);
		ldbs_close(&cp2self->cp2_super.ld_store);
		return err;
	}

	/* If this disc image has not been written to, just close it and 
	 * dispose thereof. */
	if (!self->dr_dirty)
	{
		dsk_free(cp2self->cp2_filename);
		return ldbs_close(&cp2self->cp2_super.ld_store);
	}
	/* Trying to save changes but source is read-only */
	if (cp2self->cp2_super.ld_readonly)
	{
		dsk_free(cp2self->cp2_filename);
		ldbs_close(&cp2self->cp2_super.ld_store);
		return DSK_ERR_RDONLY;
	}
	/* OK, we're ready to write back */
	cp2self->cp2_fp = fopen(cp2self->cp2_filename, "wb");
	dsk_free(cp2self->cp2_filename);
	if (!cp2self->cp2_fp)
	{
		ldbs_close(&cp2self->cp2_super.ld_store);
		return DSK_ERR_SYSERR;
	}
	dsk_report("Writing CP2 file");
	/* Write out the header */
	cp2_make_header(header);
	/* See if the release number was saved, and if so retrieve it */
	len = 4;
        err = ldbs_getblock_d(cp2self->cp2_super.ld_store, CP2_USER_BLOCK,
                &header[0x18], &len);
	if ((err != DSK_ERR_OK && err != DSK_ERR_OVERRUN) || len < 4)
	{
		/* If it failed to retrieve the release number, reset to 
		 * the default */	
		cp2_make_header(header);
	}

	if (fwrite(header, 1, 30, cp2self->cp2_fp) < 30) 
	{
		ldbs_close(&cp2self->cp2_super.ld_store);
		fclose(cp2self->cp2_fp);
		dsk_report_end();
		return DSK_ERR_SYSERR;
	}	
	cp2self->cp2_offset = 30;		/* Segment is just after header */
	cp2self->cp2_seghbuf = dsk_malloc(CP2_SEGHBUF_LEN);
	cp2self->cp2_segdbuf = dsk_malloc(CP2_SEGDBUF_LEN);

	if (!cp2self->cp2_seghbuf || !cp2self->cp2_segdbuf)
	{
		ldbs_close(&cp2self->cp2_super.ld_store);
		fclose(cp2self->cp2_fp);
		dsk_report_end();
		return DSK_ERR_NOMEM;
	}
	cp2self->cp2_segh_cur = 2;		/* Write pointers at start of buffers */
	cp2self->cp2_segd_cur = 2;
	memset(cp2self->cp2_seghbuf, 0, CP2_SEGHBUF_LEN);
	memset(cp2self->cp2_segdbuf, 0, CP2_SEGDBUF_LEN);
	
	err = ldbs_all_tracks(cp2self->cp2_super.ld_store, cp2_save_track,
				SIDES_ALT, cp2self);
	if (!err) err = cp2_flush(cp2self);
	dsk_free(cp2self->cp2_seghbuf);
	dsk_free(cp2self->cp2_segdbuf);
	if (err)
	{
		ldbs_close(&cp2self->cp2_super.ld_store);
		fclose(cp2self->cp2_fp);
		dsk_report_end();
		return err;
	}
	if (fclose(cp2self->cp2_fp))
	{
		ldbs_close(&cp2self->cp2_super.ld_store);
		dsk_report_end();
		return DSK_ERR_SYSERR;
	}
	dsk_report_end();
	return ldbs_close(&cp2self->cp2_super.ld_store);
}

